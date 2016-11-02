'''

Script that runs a series of docker containers as a integration tests.
Input:
    A narrative_runner.yaml is used to configure the environment that should be used, including
    things like the workspace to run out of, authentication information, and the environment 'prod',
    'appdev','next', the particular docker containers to Run, how many to run at a time, etc...

Output:
    This script basically runs the docker containers and uses unittest asserts() on what they
    return. So that the output can be consumed by any tool that parses unittest output

Author: Steve Chan sychan@lbl.gov

'''

import sys
import os
import time
import signal
import yaml
import unittest
import re
import logging
import xmlrunner
from docker import client


# Default module wide configs
conf = {'docker_url': 'unix://var/run/docker.sock',
        'image': 'kbase/narrative:latest',
        'entrypoint': 'headless-narrative',
        'poll_interval': 5,
        'run_env': 'ci',
        'max_running_tasks': 3,
        'delete_failed': True,
        'kill_on_timeout': False
        }

# Default yaml config file path
config_path = os.environ.get("NARRATIVE_RUNNER_CONFIG", "narrative_runner.yaml")

# prefix to be used on containernames to avoid name collision
cname_prefix = time.strftime("%m%d_%H%M%S")


class TimeoutException(Exception):
    pass


class IllegalTaskName(Exception):
    """
    Task names in the config file must be alphanumeric or _ only, so that
    the task name can be made into a descriptive functional name
    """
    pass


def TimeoutHandler(signum, frame):
    """
    Sig Alarm handler function that raises a TimeoutException
    """
    raise TimeoutException()


class NarrativeTestContainer(unittest.TestCase):
    """
    Class that serves as a container for tests generated at runtime from
    YAML description file.
    Based on various metaprogramming based unittest descriptions such as
    http://eli.thegreenplace.net/2014/04/02/dynamically-generating-python-test-cases
    http://stackoverflow.com/questions/32899/how-to-generate-dynamic-parametrized-unit-tests-in-python
    """

    longMessage = True
    cli = None
    container_list = []

    @classmethod
    def setUpClass(cls):
        """
        Simply all the test containers in parallel so that the actual test methods are simply
        checking the output
        """
        super(NarrativeTestContainer, cls).setUpClass()
        cls.cli = client.Client(base_url=conf['docker_url'])
        task_queue = conf['tasks'].keys()
        running_tasks = []
        while len(task_queue) > 0 or len(running_tasks) > 0:
            while len(running_tasks) < conf['max_running_tasks'] and len(task_queue) > 0:
                task_name = task_queue.pop()
                task = conf['tasks'][task_name]
                image = task.get('image', conf['image'])
                entrypoint = task.get('entrypoint', conf['entrypoint'])
                env = {'KB_AUTH_TOKEN': task.get('KB_AUTH_TOKEN', conf.get('KB_AUTH_TOKEN')),
                       'KB_WORKSPACE_ID': task.get('KB_WORKSPACE_ID', conf.get('KB_WORKSPACE_ID')),
                       'ENVIRON': task.get('run_env', conf.get('run_env'))}
                logging.debug("Creating image:{0} entrypoint:{1} command: '{2}' env: {3}".format(
                                image, entrypoint, task['command'], env))
                con_name = ConName(task_name)
                cls.cli.create_container(image=image, command=task['command'],
                                         entrypoint=entrypoint, environment=env, name=con_name)
                cls.container_list.append(con_name)
                cls.cli.start(con_name)
                logging.info("Started container {0}".format(con_name))
                running_tasks.append(con_name)
            finished = []
            # setup the timer for TimeoutHandler
            if 'timeout' in conf:
                signal.signal(signal.SIGALRM, TimeoutHandler)
                signal.alarm(conf['timeout'])
            try:
                while len(finished) == 0:
                    for containerId in running_tasks:
                        state = cls.cli.inspect_container(containerId)
                        if not state['State']['Running']:
                            finished.append(containerId)
                    if len(finished) == 0:
                        time.sleep(conf['poll_interval'])
            except TimeoutException:
                logging.info("Timeout triggered while waiting for containers: " +
                             ",".join(running_tasks))
                if conf['kill_on_timeout'] is True:
                    for containerId in running_tasks:
                        logging.info("Stopping container {}".format(containerId))
                        cls.cli.stop(containerId)
                    time.sleep(10)  # docker waits 10 seconds before sending SIGKILL to container
            except Exception as e:
                raise e
            for cid in finished:
                running_tasks.remove(cid)
                logging.info("Container {0} exited".format(cid))

    @classmethod
    def tearDownClass(cls):
        """
        Delete any lingering containers in the container_list. They can linger if
        there is an exception thrown by an assertion in the test and the cleanup
        code doesn't get called. This also gives us the option to leave the
        container around for debugging
        """
        if conf['delete_failed'] is True:
            for containerId in cls.container_list:
                logging.info("Removing container {}".format(containerId))
                cls.cli.remove_container(containerId)
                cls.container_list.remove(containerId)
        super(NarrativeTestContainer, cls).tearDownClass()


def MakeTestFunction(task_name, task, containerId):
    """
    Given a test task description and a containerId, return a function
    that implements that task test against the containerId
    """

    def TestTaskOutput(self):
        """
        Check the output of the container in containerId against the task['test']'
        criteria with assertions. If there are no tests associated with the task
        then the only check that applies is that exit code was 0
        """
        state = self.cli.inspect_container(containerId)
        exit_code = state['State']['ExitCode']
        if exit_code == 137:
            self.fail('Task was killed')
            # TODO Perhaps include how long the container ran before being killed?
        self.assertEquals(exit_code, task.get('tests', {}).get('exit_code', 0))
        status = True
        output = self.cli.logs(containerId)
        for test_type, param in task.get('tests', {}).iteritems():
            if test_type == "str_match":
                status = status and (output.find(param) >= 0)
        self.assertTrue(status, msg="container output: {}".format(output[0:80]))
        self.cli.remove_container(containerId)
        self.container_list.remove(containerId)

    return TestTaskOutput


def ConName(task_name):
    """
    We use these container names in 2 different places, make sure we generate them
    the same way
    """
    return "{0}_{1}".format(cname_prefix, task_name)


def GenerateTestTasks(conf):
    """
    Generate the Unittest test tasks in a batch
    """
    for task_name, task in conf['tasks'].iteritems():
            test_func = MakeTestFunction(task_name, task, ConName(task_name))
            setattr(NarrativeTestContainer, 'test_{0}'.format(task_name), test_func)


def ValidateTaskNames(conf):
    # verify that all task_names are alphanumeric+_ and can
    # be used in a function name
    legit_name = re.compile('^\w+$')
    for task_name in conf['tasks'].keys():
        if not legit_name.match(task_name):
            raise IllegalTaskName('Task names must match [a-zA-Z0-9_]+: "{}"'.format(task_name))


def main():
    with open(config_path, 'r') as f:
        conf2 = yaml.load(f)
    conf.update(conf2)
    ValidateTaskNames(conf)

    # Set the logging loglevel based on the "loglevel" setting in the yaml file
    if 'loglevel' in conf:
        numeric_level = getattr(logging, conf['loglevel'].upper(), None)
        if not isinstance(numeric_level, int):
            raise ValueError('Invalid log level: %s' % conf['loglevel'])
        logging.basicConfig(level=numeric_level)
    # Generate test methods for all the tasks
    GenerateTestTasks(conf)

    if 'xml_output' in conf:
        unittest.main(testRunner=xmlrunner.XMLTestRunner(output=conf['xml_output']))
    else:
        unittest.main()

if __name__ == '__main__':
    sys.exit(int(main() or 0))
