from datetime import datetime
import logging
import re
import requests
import time
from threading import Thread

from django.conf import settings
from django.db import models
from django.db.models import Count, Max
from django.core.exceptions import ValidationError
from jsonfield import JSONField

from api.models import UuidAuditedModel, log_event
from api.utils import generate_app_name, app_build_type
from api.models.release import Release
from api.models.config import Config
from api.models.container import Container
from api.models.domain import Domain

from scheduler import KubeHTTPException, KubeException

logger = logging.getLogger(__name__)


# http://kubernetes.io/v1.1/docs/design/identifiers.html
def validate_id_is_docker_compatible(value):
    """
    Check that the value follows the kubernetes name constraints
    """
    match = re.match(r'^[a-z0-9-]+$', value)
    if not match:
        raise ValidationError("App name can only contain a-z (lowercase), 0-9 and hypens")


def validate_app_structure(value):
    """Error if the dict values aren't ints >= 0"""
    try:
        if any(int(v) < 0 for v in value.values()):
            raise ValueError("Must be greater than or equal to zero")
    except ValueError as err:
        raise ValidationError(err)


def validate_reserved_names(value):
    """A value cannot use some reserved names."""
    if value in settings.DEIS_RESERVED_NAMES:
        raise ValidationError('{} is a reserved name.'.format(value))


class Pod(dict):
    pass


class App(UuidAuditedModel):
    """
    Application used to service requests on behalf of end-users
    """

    owner = models.ForeignKey(settings.AUTH_USER_MODEL)
    id = models.SlugField(max_length=24, unique=True, null=True,
                          validators=[validate_id_is_docker_compatible,
                                      validate_reserved_names])
    structure = JSONField(default={}, blank=True, validators=[validate_app_structure])

    class Meta:
        permissions = (('use_app', 'Can use app'),)

    def save(self, *args, **kwargs):
        if not self.id:
            self.id = generate_app_name()
            while App.objects.filter(id=self.id).exists():
                self.id = generate_app_name()

        application = super(App, self).save(**kwargs)

        # create all the required resources
        self.create(*args, **kwargs)

        return application

    def __str__(self):
        return self.id

    def _get_job_id(self, container_type):
        app = self.id
        release = self.release_set.latest()
        version = "v{}".format(release.version)
        job_id = "{app}-{version}-{container_type}".format(**locals())
        return job_id

    def _get_command(self, container_type):
        try:
            # if this is not procfile-based app, ensure they cannot break out
            # and run arbitrary commands on the host
            # FIXME: remove slugrunner's hardcoded entrypoint
            release = self.release_set.latest()
            if release.build.dockerfile or not release.build.sha:
                return "bash -c '{}'".format(release.build.procfile[container_type])
            else:
                return 'start {}'.format(container_type)
        # if the key is not present or if a parent attribute is None
        except (KeyError, TypeError, AttributeError):
            # handle special case for Dockerfile deployments
            return '' if container_type == 'cmd' else 'start {}'.format(container_type)

    def log(self, message, level=logging.INFO):
        """Logs a message in the context of this application.

        This prefixes log messages with an application "tag" that the customized deis-logspout will
        be on the lookout for.  When it's seen, the message-- usually an application event of some
        sort like releasing or scaling, will be considered as "belonging" to the application
        instead of the controller and will be handled accordingly.
        """
        logger.log(level, "[{}]: {}".format(self.id, message))

    def create(self, *args, **kwargs):
        """
        Create a application with an initial config, release, domain
        and k8s resource if needed
        """
        try:
            cfg = self.config_set.latest()
        except Config.DoesNotExist:
            cfg = Config.objects.create(owner=self.owner, app=self)

        # Only create if no release can be found
        try:
            rel = self.release_set.latest()
        except Release.DoesNotExist:
            rel = Release.objects.create(
                version=1, owner=self.owner, app=self,
                config=cfg, build=None
            )

        # create required minimum resources in k8s for the application
        self._scheduler.create(self.id)

        # Attach the platform specific application sub domain to the k8s service
        # Only attach it on first release in case a customer has remove the app domain
        if rel.version == 1 and not Domain.objects.filter(domain=self.id).exists():
            Domain(owner=self.owner, app=self, domain=self.id).save()

    def delete(self, *args, **kwargs):
        """Delete this application including all containers"""
        try:
            # attempt to remove containers from the scheduler
            self._scheduler.destroy(self.id)
            self._destroy_containers([c for c in self.container_set.exclude(type='run')])
        except RuntimeError:
            pass
        self._clean_app_logs()
        return super(App, self).delete(*args, **kwargs)

    def restart(self, **kwargs):  # noqa
        """
        Restart found pods by deleting them (RC will recreate).
        Wait until they are all drained away and RC has gotten to a good state
        """
        try:
            # Resolve single pod name if short form (worker-asdfg) is passed
            if 'name' in kwargs and kwargs['name'].count('-') == 1:
                if 'release' not in kwargs or kwargs['release'] is None:
                    release = self.release_set.latest()
                else:
                    release = self.release_set.get(version=kwargs['release'])

                version = "v{}".format(release.version)
                kwargs['name'] = '{}-{}-{}'.format(kwargs['id'], version, kwargs['name'])

            # Iterate over RCs to get total desired count if not a single item
            desired = 1
            if 'name' not in kwargs:
                desired = 0
                labels = self._scheduler_filter(**kwargs)
                controllers = self._scheduler._get_rcs(kwargs['id'], labels=labels).json()['items']
                for controller in controllers:
                    desired += controller['spec']['replicas']
        except KubeException:
            # Nothing was found
            return []

        try:
            for pod in self.list_pods(**kwargs):
                # This function verifies the delete. Gives pod 30 seconds
                self._scheduler._delete_pod(self.id, pod['name'])
        except Exception as e:
            err = "warning, some pods failed to stop:\n{}".format(str(e))
            log_event(self, err, logging.WARNING)

        # Wait for pods to start
        try:
            timeout = 300  # 5 minutes
            elapsed = 0
            while True:
                # timed out
                if elapsed >= timeout:
                    raise RuntimeError('timeout - 5 minutes have passed and pods are not up')

                # restarting a single pod behaves differently, fetch the *newest* pod
                # and hope it is the right one. Comes back sorted
                if 'name' in kwargs:
                    del kwargs['name']
                    pods = self.list_pods(**kwargs)
                    # Add in the latest name
                    kwargs['name'] = pods[0]['name']
                    pods = pods[0]

                actual = 0
                for pod in self.list_pods(**kwargs):
                    if pod['state'] == 'up':
                        actual += 1

                if desired == actual:
                    break

                elapsed += 5
                time.sleep(5)

        except Exception as e:
            err = "warning, some pods failed to start:\n{}".format(str(e))
            log_event(self, err, logging.WARNING)

        # Return the new pods
        pods = self.list_pods(**kwargs)
        return pods

    def restart_old(self, **kwargs):
        to_restart = self.container_set.all()
        if kwargs.get('type'):
            to_restart = to_restart.filter(type=kwargs.get('type'))
        if kwargs.get('num'):
            to_restart = to_restart.filter(num=kwargs.get('num'))
        self._restart_containers(to_restart)
        return to_restart

    def _clean_app_logs(self):
        """Delete application logs stored by the logger component"""
        try:
            url = 'http://{}:{}/logs/{}'.format(settings.LOGGER_HOST,
                                                settings.LOGGER_PORT, self.id)
            requests.delete(url)
        except Exception as e:
            # Ignore errors deleting application logs.  An error here should not interfere with
            # the overall success of deleting an application, but we should log it.
            err = 'Error deleting existing application logs: {}'.format(e)
            log_event(self, err, logging.WARNING)

    def scale(self, user, structure):  # noqa
        """Scale containers up or down to match requested structure."""
        # use create to make sure minimum resources are created
        self.create()

        if self.release_set.latest().build is None:
            raise EnvironmentError('No build associated with this release')

        requested_structure = structure.copy()
        release = self.release_set.latest()
        # test for available process types
        available_process_types = release.build.procfile or {}
        for container_type in requested_structure:
            if container_type == 'cmd':
                continue  # allow docker cmd types in case we don't have the image source

            if container_type not in available_process_types:
                raise EnvironmentError(
                    'Container type {} does not exist in application'.format(container_type))

        msg = '{} scaled containers '.format(user.username) + ' '.join(
            "{}={}".format(k, v) for k, v in list(requested_structure.items()))
        log_event(self, msg)
        # iterate and scale by container type (web, worker, etc)
        changed = False
        to_add, to_remove = [], []
        scale_types = {}

        # iterate on a copy of the container_type keys
        for container_type in list(requested_structure.keys()):
            containers = list(self.container_set.filter(type=container_type).order_by('created'))
            # increment new container nums off the most recent container
            results = self.container_set.filter(type=container_type).aggregate(Max('num'))
            container_num = (results.get('num__max') or 0) + 1
            requested = requested_structure.pop(container_type)
            diff = requested - len(containers)
            if diff == 0:
                continue

            changed = True
            scale_types[container_type] = requested
            while diff < 0:
                c = containers.pop()
                to_remove.append(c)
                diff += 1

            while diff > 0:
                # create a database record
                c = Container.objects.create(owner=self.owner,
                                             app=self,
                                             release=release,
                                             type=container_type,
                                             num=container_num)
                to_add.append(c)
                container_num += 1
                diff -= 1

        if changed:
            if "scale" in dir(self._scheduler):
                self._scale_containers(scale_types, to_remove)
            else:
                if to_add:
                    self._start_containers(to_add)

                if to_remove:
                    self._destroy_containers(to_remove)

        # save new structure to the database
        vals = self.container_set.exclude(type='run').values(
            'type').annotate(Count('pk')).order_by()
        new_structure = structure.copy()
        new_structure.update({v['type']: v['pk__count'] for v in vals})
        self.structure = new_structure
        self.save()
        return changed

    def _scale_containers(self, scale_types, to_remove):
        release = self.release_set.latest()
        build_type = app_build_type(release)
        for scale_type in scale_types:
            image = release.image
            version = "v{}".format(release.version)
            kwargs = {
                'memory': release.config.memory,
                'cpu': release.config.cpu,
                'tags': release.config.tags,
                'envs': release.config.values,
                'version': version,
                'num': scale_types[scale_type],
                'app_type': scale_type,
                'build_type': build_type,
                'healthcheck': release.config.healthcheck()
            }

            job_id = self._get_job_id(scale_type)
            command = self._get_command(scale_type)
            try:
                self._scheduler.scale(
                    namespace=self.id,
                    name=job_id,
                    image=image,
                    command=command,
                    **kwargs
                )
            except Exception as e:
                err = '{} (scale): {}'.format(job_id, e)
                log_event(self, err, logging.ERROR)
                raise
        [c.delete() for c in to_remove]

    def _start_containers(self, to_add):
        """Creates and starts containers via the scheduler"""
        if not to_add:
            return
        create_threads = [Thread(target=c.create) for c in to_add]
        start_threads = [Thread(target=c.start) for c in to_add]
        [t.start() for t in create_threads]
        [t.join() for t in create_threads]
        if any(c.state != 'created' for c in to_add):
            err = 'aborting, failed to create some containers'
            log_event(self, err, logging.ERROR)
            self._destroy_containers(to_add)
            raise RuntimeError(err)
        [t.start() for t in start_threads]
        [t.join() for t in start_threads]
        if set([c.state for c in to_add]) != set(['up']):
            err = 'warning, some containers failed to start'
            log_event(self, err, logging.WARNING)

    def _restart_containers(self, to_restart):
        """Restarts containers via the scheduler"""
        if not to_restart:
            return
        stop_threads = [Thread(target=c.stop) for c in to_restart]
        start_threads = [Thread(target=c.start) for c in to_restart]
        [t.start() for t in stop_threads]
        [t.join() for t in stop_threads]
        if any(c.state != 'created' for c in to_restart):
            err = 'warning, some containers failed to stop'
            log_event(self, err, logging.WARNING)
        [t.start() for t in start_threads]
        [t.join() for t in start_threads]
        if any(c.state != 'up' for c in to_restart):
            err = 'warning, some containers failed to start'
            log_event(self, err, logging.WARNING)

    def _destroy_containers(self, to_destroy):
        """Destroys containers via the scheduler"""
        if not to_destroy:
            return

        # for mock scheduler
        if "scale" not in dir(self._scheduler):
            destroy_threads = [Thread(target=c.destroy) for c in to_destroy]
            [t.start() for t in destroy_threads]
            [t.join() for t in destroy_threads]
            [c.delete() for c in to_destroy if c.state == 'destroyed']
            if any(c.state != 'destroyed' for c in to_destroy):
                err = 'aborting, failed to destroy some containers'
                log_event(self, err, logging.ERROR)
                raise RuntimeError(err)
        else:
            [c.delete() for c in to_destroy]

    def deploy(self, user, release):
        """Deploy a new release to this application"""
        # use create to make sure minimum resources are created
        self.create()

        existing = self.container_set.exclude(type='run')
        new = []
        scale_types = set()
        for e in existing:
            n = e.clone(release)
            n.save()
            new.append(n)
            scale_types.add(e.type)

        if new and "deploy" in dir(self._scheduler):
            self._deploy_app(scale_types, release, existing)
        else:
            self._start_containers(new)

            # destroy old containers
            if existing:
                self._destroy_containers(existing)

        # perform default scaling if necessary
        if self.structure == {} and release.build is not None:
            self._default_scale(user, release)

    def _deploy_app(self, scale_types, release, existing):
        build_type = app_build_type(release)
        for scale_type in scale_types:
            image = release.image
            version = "v{}".format(release.version)
            kwargs = {
                'memory': release.config.memory,
                'cpu': release.config.cpu,
                'tags': release.config.tags,
                'envs': release.config.values,
                'num': 0,  # Scaling up happens in a separate operation
                'version': version,
                'app_type': scale_type,
                'build_type': build_type,
                'healthcheck': release.config.healthcheck()
            }

            job_id = self._get_job_id(scale_type)
            command = self._get_command(scale_type)
            try:
                self._scheduler.deploy(
                    namespace=self.id,
                    name=job_id,
                    image=image,
                    command=command,
                    **kwargs)
            except Exception as e:
                err = '{} (deploy): {}'.format(job_id, e)
                log_event(self, err, logging.ERROR)
                raise
        [c.delete() for c in existing]

    def _default_scale(self, user, release):
        """Scale to default structure based on release type"""
        # if there is no SHA, assume a docker image is being promoted
        if not release.build.sha:
            structure = {'cmd': 1}

        # if a dockerfile exists without a procfile, assume docker workflow
        elif release.build.dockerfile and not release.build.procfile:
            structure = {'cmd': 1}

        # if a procfile exists without a web entry, assume docker workflow
        elif release.build.procfile and 'web' not in release.build.procfile:
            structure = {'cmd': 1}

        # default to heroku workflow
        else:
            structure = {'web': 1}

        if "scale" in dir(self._scheduler):
            self._deploy_app(structure, release, [])
        else:
            for key, num in structure.items():
                c = Container.objects.create(owner=self.owner,
                                             app=self,
                                             release=release,
                                             type=key,
                                             num=num)
                self._start_containers([c])

    def logs(self, log_lines=str(settings.LOG_LINES)):
        """Return aggregated log data for this application."""
        try:
            url = "http://{}:{}/logs/{}?log_lines={}".format(settings.LOGGER_HOST,
                                                             settings.LOGGER_PORT,
                                                             self.id, log_lines)
            r = requests.get(url)
        # Handle HTTP request errors
        except requests.exceptions.RequestException as e:
            logger.error("Error accessing deis-logger using url '{}': {}".format(url, e))
            raise e

        # Handle logs empty or not found
        if r.status_code == 204 or r.status_code == 404:
            logger.info("GET {} returned a {} status code".format(url, r.status_code))
            raise EnvironmentError('Could not locate logs')

        # Handle unanticipated status codes
        if r.status_code != 200:
            logger.error("Error accessing deis-logger: GET {} returned a {} status code"
                         .format(url, r.status_code))
            raise EnvironmentError('Error accessing deis-logger')

        # cast content to string since it comes as bytes via the requests object
        return str(r.content)

    def run(self, user, command):
        """Run a one-off command in an ephemeral app container."""
        if self.release_set.latest().build is None:
            raise EnvironmentError('No build associated with this release to run this command')
        # TODO: add support for interactive shell
        msg = "{} runs '{}'".format(user.username, command)
        log_event(self, msg)
        c_num = max([c.num for c in self.container_set.filter(type='run')] or [0]) + 1

        # create database record for run process
        c = Container.objects.create(owner=self.owner,
                                     app=self,
                                     release=self.release_set.latest(),
                                     type='run',
                                     num=c_num)
        # SECURITY: shell-escape user input
        escaped_command = command.replace("'", "'\\''")
        return c.run(escaped_command)

    def list_pods(self, *args, **kwargs):
        """Used to list basic information about pods running for a given application"""
        try:
            labels = self._scheduler_filter(**kwargs)

            # in case a singular pod is requested
            if 'name' in kwargs:
                pods = [self._scheduler._get_pod(self.id, kwargs['name']).json()]
            else:
                pods = self._scheduler._get_pods(self.id, labels=labels).json()['items']

            data = []
            for p in pods:
                # specifically ignore run pods
                if p['metadata']['labels']['type'] == 'run':
                    continue

                item = Pod()
                item['name'] = p['metadata']['name']
                item['state'] = self._scheduler.resolve_state(p).name
                item['release'] = p['metadata']['labels']['version']
                item['type'] = p['metadata']['labels']['type']
                if 'startTime' in p['status']:
                    started = p['status']['startTime']
                else:
                    started = str(datetime.utcnow().strftime(settings.DEIS_DATETIME_FORMAT))
                item['started'] = started

                data.append(item)

            # sorting so latest start date is first
            data.sort(key=lambda x: x['started'], reverse=True)

            return data
        except KubeHTTPException as e:
            pass
        except Exception as e:
            err = '(list pods): {}'.format(e)
            log_event(self, err, logging.ERROR)
            raise

    def _scheduler_filter(self, **kwargs):
        labels = {'app': self.id}

        # always supply a version, either latest or a specific one
        if 'release' not in kwargs or kwargs['release'] is None:
            release = self.release_set.latest()
        else:
            release = self.release_set.get(version=kwargs['release'])

        version = "v{}".format(release.version)
        labels.update({'version': version})

        if 'type' in kwargs:
            labels.update({'type': kwargs['type']})

        return labels