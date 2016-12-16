# ContainerRunner

Unittest based module that runs docker containers and examines the output/exit codes to determine success or failure.

ContainerRunner is simply a script that

1. reads a YAML configuration file specified in the environment variable CONTAINER_RUNNER_CONFIG
1. runs the list of docker images in the file using the parameters specified for each test task
1. dynamically generates test functions to examine the output of each of those containers, and inserts them into the TestCase class
1. hands off execution to the Unittest framework by running the TestCase 

The ContainerRunner script defaults to using "headless-narrative" as the entrypoint if none is specified. This is because the
default use case is for testing [KBase headless narratives](https://github.com/kbase/narrative/blob/develop/docs/HeadlessTesting.md)

There are only 2 real configuration items for this script - the location of the configuration file, via
the CONTAINER_RUNNER_CONFIG shell variable, and the contents of the configuration file itself.

# The Container Runner Config file

This is a YAML file that describes the run environment for the containers, the images to be used
for the containers, and the tests to be run on the output of the containers for the test assertion.

Here is an excerpt of the sample YAML file from this repo with annotations:
~~~
max_running_tasks : 6  # max number of docker containers running simultaneously 
delete_failed: True    # delete containers that fail assertion. Defaults to false
loglevel: info         # log level to be set in python logging framework
#xml_output: xml_output # what directory to write XML output to, if not set don't write XML
timeout: 60   # How long to wait for containers to complete before timing out
kill_on_timeout: True  # Do we stop the containers on timeout, or merely warn?
tasks :       # The dictionary of test tasks. Describes all the images to run
    hello_world: # Name of the task, will be displayed as part of the logs, should be informative
        command: "hello world"  # What is the 'command' that is passed to the entrypoint
        image : ubuntu:latest   # What docker image to run
        entrypoint : /bin/echo  # What is the entrypoint? In this case, we use /bin/echo
        tests:                  # List of tests to run, multiple entries are AND together
            str_match: hello    # Perform a basic string match for "hello" in the container output
    goodbye2:
        command: /etc/passwd
        image : ubuntu:latest
        entrypoint : /usr/bin/tail
        tests: # This set of tests requires both exit code 127 AND "godbye" string
            str_match: "godbye"
            exit_code: 127
    stop_dave:
        command: "Will you stop, Dave?"
        image : ubuntu:latest
        entrypoint : /bin/echo
        tests:
            regex_match: "^Will.*Dave"  # Use Python regex for match
    timeout:
        command: "90"
        image : ubuntu:latest
        env:
            KB_CELL_TIMEOUT: 10  # The KB_CELL_TIMEOUT env variable controls individual cell timeout in narrative
            SSH_AUTH_PORT: /tmp/blah # An example of setting the environment var SSH_AUTH_PORT - to a silly value
        entrypoint : /bin/sleep
~~~

Here are the directives that can be includes in the YAML file:

## These directives should be in the outermost scope of the YAML file

### docker_url

The URL to the docker service that handles requests. If authentication is required you
will have to put it into your ~/.docker configuration file(s). The
[docker python sdk libraries](https://github.com/docker/docker-py/tree/1.10.6-release) are used,
so it may be helpful to review those docs if custom configuration is needed. Defaults to
unix://var/run/docker.sock

### max_running_tasks

How many docker containers to have running at any given time. Because narratives
typically are blocked, waiting on backend tasks to complete, it is reasonable to run several of them
simultaneously. This sets the maximum number that can be runnning at a time. Defaults to 3

### poll_interval

How frequently should the task runner poll job status to see if anything completed. This value
is in seconds, and defaults to 5 seconds

### delete_failed

To avoid cluttering up the host where tests are run, all tests have their containers
deleted after being run. But if you are testing/debugging a problem, you may prefer to just have
failed tests leave their containers around for debugging. Defaults to 'true'

### timeout

How long do we sleep before triggering a timeout? Note that this isn't the timeout interval per
container, it is just how long since *any* container has finished running. Within each container, there
are already per-cell timeouts which will trigger internal timeouts and eventually shutdown the container,
irrespective of what the running script is waiting for

### kill_on_timeout

When a timeout is triggered, do we will the running containers? Defaults to yes - but you can set it to
no and only generate a warning, allowing the container to keep running until either the container
terminates.

### loglevel

This script uses the standard python logger, and [loglevel](https://docs.python.org/2/library/logging.html#levels)
is passed directly to the python logger to determine log verbosity.

### tasks

This attribute should contain a dict of which tasks to run. The attributes one level below "tasks" are
treated as the names of the tasks to be run.

## These directives can be declared in the outermost scope, or in a task scope.

If declared in the outermost scope they are inherited, if they are in the task scope it will take
precedence over the global scope

### KB_AUTH_TOKEN

This is the KB_AUTH_TOKEN as used by most KBase services. It is passed into the docker container
as an environment variable of the same name

### run_env

This is the environment to run service in - "ci", "prod", "appdev", "next", etc... This value is
passed into the running container as the environment variable ENVIRON, which is parsed by the
headless_narrative script. Defaults to "ci"

### entrypoint

The entrypoint passed into the docker run command. Defaults to "headless-narrative"

### image

What docker image to run. Defaults to kbase/narrative:latest

## These directives can only be declared with a specific task scope

### command

What command to be passed in to the docker run command

### env

A dictionary that is used to set arbitrary environment variables in the running container. Note that this
setting has the highest precedence and will overwrite things such as run_env and KB_AUTH_TOKEN