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
import time
import signal
import yaml
import unittest
import re
import logging
from docker import client


# Default module wide configs
conf = {'docker_url': 'unix://var/run/docker.sock',
        'image': 'kbase/narrative:latest',
        'entrypoint': 'headless-narrative',
        'poll_interval': 5,
        'run_env': 'ci',
        'debug': False,
        'max_running_tasks': 1
        }

# Default yaml config file path
config_path = "narrative_runner.yaml"

# Module wide docker client handle
cli = None
# Number of seconds between polls of the container state

# List of containerIds started by this narrative runner instanc
container_list = []


class TimeoutException(Exception):
    pass


def TimeoutHandler(signum, frame):
    """
    Sig Alarm handler function that raises a TimeoutException
    """
    raise TimeoutException()


def StartContainer(image, command, entry, env):
    """
    Basically the equivalent of the docker run command. Docker API doesn't have a direct
    API method that matches docker run

    Returns the container ID or else re-raises the error that the docker API throws.
    """

    logging.debug("Creating image:{0} entrypoint:{1} command: '{2}' environment: {3}".format(image,
                  command, entry, env))
    container = cli.create_container(image=image,
                                     command=command,
                                     entrypoint=entry,
                                     environment=env)
    cli.start(container['Id'])
    container_list.append(container['Id'])
    return(container['Id'])


def WaitUntilStopped(containerIds, timeout=600):
    """
    Takes an array of container IDs and polls them every poll_interval seconds to see if
    any have finished. Note that if any of the containerIds are already stopped, this
    will end up returning immediately.
    Will timeout after 10 minutes and return, but that can be overridden via timeout param
    A timeout set to None will never timeout.
    """

    finished = []
    # setup the timer for TimeoutHandler
    if timeout is not None:
        signal.signal(signal.SIGALRM, TimeoutHandler)
        signal.alarm(timeout)
    try:
        while len(finished) == 0:
            for containerId in containerIds:
                state = cli.inspect_container(containerId)
                if not state['State']['Running']:
                    finished.append(containerId)
            if len(finished) == 0:
                time.sleep(conf['poll_interval'])
    except TimeoutException:
        pass
    except Exception as e:
        raise e

    return(finished)


def RemoveContainers(containerIds):
    """
    Delete the containers in the containerIds list and take it out
    of the list of containers created by this module
    """
    for containerId in containerIds:
        logging.info("Removing container {}".format(containerId))
        cli.remove_container(containerId)
        container_list.remove(containerId)


def TestTaskOutput(task, cid):
    """
    Check the output of the container in cid against the test criteria in task
    and return (status,output) as a boolean as to whether the tests pass and
    output as the return from the container
    """
    status = True
    output = cli.logs(cid)
    for test_type, param in task['tests'].iteritems():
        if test_type == "str_match":
            status = status and (output.find(param) >= 0)
    return(status, output)


class IllegalTaskName(Exception):
    """
    Task names in the config file must be alphanumeric or _ only, so that
    the task name can be made into a descriptive functional name
    """
    pass


class ContainerBadExitCode(Exception):
    """
    Exception raised when the exit code of the program in the container
    is non-Zero
    """
    pass


class NarrativeTestContainer(unittest.TestCase):
    """
    Class that serves as a container for tests generated at runtime from
    YAML description file.
    Based on various metaprogramming based unittest descriptions such as
    http://eli.thegreenplace.net/2014/04/02/dynamically-generating-python-test-cases
    http://stackoverflow.com/questions/32899/how-to-generate-dynamic-parametrized-unit-tests-in-python
    """

    longMessage = True


def MakeTestFunction(task_name, task, containerId):
    """
    Given a test task description and a containerId, return a function
    that implements that task test against the containerId
    """

    def TestTaskOutput(self):
        """
        Check the output of the container in cid against the test criteria in task
        and return (status,output) as a boolean as to whether the tests pass and
        output as the return from the container
        """
        state = cli.inspect_container(containerId)
        exit_code = state['State']['ExitCode']
        self.assertEquals(exit_code, task['tests'].get('exit_code', 0))
        status = True
        output = cli.logs(containerId)
        for test_type, param in task['tests'].iteritems():
            if test_type == "str_match":
                status = status and (output.find(param) >= 0)
        self.assertTrue(status, msg="container output: {}".format(output[0:80]))
        cli.remove_container(containerId)

    return TestTaskOutput


def main():
    global cli
    with open(config_path, 'r') as f:
        conf2 = yaml.load(f)
    conf.update(conf2)

    # Set the logging loglevel based on the "loglevel" setting in the yaml file
    if 'loglevel' in conf:
        numeric_level = getattr(logging, conf['loglevel'].upper(), None)
        if not isinstance(numeric_level, int):
            raise ValueError('Invalid log level: %s' % conf['loglevel'])
        logging.basicConfig(level=numeric_level)

    cli = client.Client(base_url=conf['docker_url'])

    # verify that all task_names are alphanumeric+_ and can
    # be used in a function name
    legit_name = re.compile('^\w+$')
    for task_name in conf['tasks'].keys():
        if not legit_name.match(task_name):
            raise IllegalTaskName('Task names must match [a-zA-Z0-9_]+: "{}"'.format(task_name))
    # Run all the tasks and generate a unittest test function for each task which
    # examines the container logs. We finish running all the tasks before calling unittest
    # main() function. This is necessary to run the containers asynchronously and in parallel
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
                   'environ': task.get('run_env', conf.get('run_env'))}
            logging.info("Running task {0}".format(task_name))
            cid = StartContainer(image, task['command'], entrypoint, env)
            logging.info("Started container {0}".format(cid))
            running_tasks.append(cid)
            test_func = MakeTestFunction(task_name, task, cid)
            setattr(NarrativeTestContainer, 'test_{0}_task'.format(task_name), test_func)
        fin = WaitUntilStopped(running_tasks)
        for cid in fin:
            running_tasks.remove(cid)
            logging.info("Container {0} exited".format(cid))
    unittest.main(verbosity=2)

if __name__ == '__main__':
    sys.exit(int(main() or 0))
