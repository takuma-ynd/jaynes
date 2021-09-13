import base64
import glob
import os
import sys
import tempfile
from textwrap import dedent
from types import SimpleNamespace
from typing import Union

from jaynes.client import JaynesClient
from jaynes.helpers import cwd_ancestors, omit, hydrate, snake2camel
from jaynes.runners import Docker, Simple
from jaynes.shell import ck
from jaynes.templates import gce_terminate, ec2_terminate, ec2_tag_instance, ssh_remote_exec


class Jaynes:
    def __init__(self, mounts=None, runner=None):
        self.mounts = mounts or []
        self.set_runner(runner)

    def set_runner(self, runner: Union[Docker, Simple]):
        self.runner = runner

    def set_mount(self, *mounts):
        self.mounts = mounts

    _uploaded = []

    def upload_mount(self, verbose=None, **host, ):
        for mount in self.mounts:
            if mount in self._uploaded:
                print('this package is already uploaded')
            else:
                self._uploaded.append(mount)
                mount.upload(verbose=verbose, **host)

    # def run_local_setup(self, verbose=False):
    #     for m in self.mounts:
    #         self.upload_mount(m)

    host_unpacked = None

    def make_host_unpack_script(self, launch_dir="/tmp/jaynes-mount", delay=None, root_config=None, **_):
        """

        :param launch_dir:
        :param delay:
        :param root_config: a setup script **before** anything is ran.
        :param _:
        :return:
        """
        # the log_dir is primarily used for the run script. Therefore it should use ued here instead.
        log_path = os.path.join(launch_dir, "jaynes-launch.log")
        error_path = os.path.join(launch_dir, "jaynes-launch.err.log")

        all_setup = "\n".join(
            [m.host_setup for m in self.mounts if hasattr(m, "host_setup") and m.host_setup]
        )

        host_unpack_script = dedent(f"""
        #!/bin/bash
        # to allow process substitution
        set +o posix
        {root_config or ""}
        mkdir -p {launch_dir}
        {{
            {all_setup}
            {f"sleep {delay}" if delay else ""}
        }} > >(tee -a {log_path}) 2> >(tee -a {error_path} >&2)
        """).strip()

        return host_unpack_script

    def launch_local_docker(self, log_dir="/tmp/jaynes-mount", delay=None, verbose=False, dry=False, root_config=None):
        # the log_dir is primarily used for the run script. Therefore it should use ued here instead.
        log_path = os.path.join(log_dir, "jaynes-launch.log")
        error_path = os.path.join(log_dir, "jaynes-launch.err.log")

        upload_script = '\n'.join(
            [m.upload_script for m in self.mounts if hasattr(m, "upload_script") and m.upload_script]
        )
        host_setup = "" if self.host_unpacked else "\n".join(
            [m.host_setup for m in self.mounts if hasattr(m, "host_setup") and m.host_setup]
        )

        remote_script = dedent(f"""
        #!/bin/bash
        # to allow process substitution
        set +o posix
        {root_config or ""}
        mkdir -p {log_dir}
        {{
            {host_setup}

            # upload_script
            {upload_script}

            {self.runner.setup_script}
            {self.runner.run_script}
            
            {f"sleep {delay}" if delay else ""}
        }} > >(tee -a {log_path}) 2> >(tee -a {error_path} >&2)
        """).strip()
        if verbose:
            print(remote_script)
        if not dry:
            ck(remote_script, shell=True)
        return self

    launch_script = None

    def make_host_script(self,
                         launch_dir="~/jaynes-launch",
                         pipe_out=None,
                         setup=None, terminate_after=False,
                         delay=None, instance_name=None,
                         root_config=None,
                         type=None,
                         **_):
        """
        function to make the host script

        :param launch_dir: 
        :param sudo:
        :param terminate_after:
        :param delay:
        :param instance_name: less than 128 ascii characters
        :return:
        """
        log_setup = dedent(f"""
        mkdir -p {launch_dir}
        JAYNES_LAUNCH_DIR={launch_dir}
        """)

        if not pipe_out:
            log_path = os.path.join(launch_dir, "jaynes-launch.log")
            error_path = os.path.join(launch_dir, "jaynes-launch.err.log")
            pipe_out = pipe_out or f""" > >(tee -a {log_path}) 2> >(tee -a {error_path} >&2)"""

        upload_script = '\n'.join(
            [m.upload_script for m in self.mounts if hasattr(m, "upload_script") and m.upload_script]
        )
        # does not unpack if the self.host_unpack_script has already been generated.
        host_unpack_script = "" if self.host_unpacked else "\n".join(
            [m.host_setup for m in self.mounts if hasattr(m, "host_setup") and m.host_setup]
        )
        if instance_name:
            assert len(instance_name) <= 128, "Error: ws limits instance tag to 128 unicode characters."

        # NOTE: path.join is running on local computer, so it might not be quite right if remote is say windows.
        # NOTE: dedent is required by aws EC2.
        terminate_commands = ""
        if terminate_after:
            if type == "ec2":
                terminate_commands = ec2_terminate(delay)
            elif type == "gce":
                terminate_commands = gce_terminate(delay)
            else:
                raise NotImplementedError(f"terminate_after is not supported with {type}")

        self.launch_script = f"""
#!/bin/bash
# to allow process substitution
set +o posix
{root_config or ''}
{log_setup or ''}
{{
            # launch.setup script
            {setup or ""}
{ec2_tag_instance(instance_name) if type == "ec2" and instance_name else ""}
{host_unpack_script}
            # upload_script from within the host.
{upload_script}
            # runner.setup script
{self.runner.setup_script}
            # run script
{self.runner.run_script}
            # post script
{self.runner.post_script}
{terminate_commands}
}} {pipe_out or ""}
""".strip()
        # }} > {log_path} 2> {error_path} &
        # }} > >({tee_string}{log_path}) 2> >({tee_string}{error_path} >&2)

        return self

    def launch_ssh(self, ip, port=None, username="ubuntu", pem=None, profile=None,
                   password=None, sudo=False, cleanup=True, block=False, console_mode=False, dry=False,
                   verbose=False, **_):
        """
        run launch_script remotely by ip_address. First saves the run script locally as a file, then use
        scp to transfer the script to remote instance then run.

        :param username:
        :param ip:
        :param port:
        :param pem:
        :param sudo:
        :param cleanup: whether to attach clean up script at the end of the launch scrip
        :param block: whether wait for p.communication after calling. Blocks further execution.
        :param console_mode: do not block, do not use stdout.pipe when running from ipython console.
        :param profile: Suppose you want to run bash as a different user after ssh in, you can use this option to
                        pass in a different user name. This is inserted in the ssh boostrapping command, so the script
                        you run will not be affected (and will take up this user's login envs instead).
        :param password: The password for the user in case it is needed.
        :param dry:
        :param verbose:
        :return:
        """
        # todo: is this still used?
        tf = tempfile.NamedTemporaryFile(prefix="jaynes_launcher-", suffix=".sh", delete=False)
        with open(tf.name, 'w') as f:
            _ = os.path.basename(tf.name)  # fixit: does kill require sudo?
            cleanup_script = dedent(f"""
            PROCESSES=$(ps aux | grep '[{_[0]}]{_[1:]}' | awk '{{print $2}}')
            if [ $PROCESSES ] 
            then
            {"sudo " if sudo else ""}kill $PROCESSES
            {f"echo 'cleaned up after {_}" if verbose else ""}
            fi 
            """)
            f.write(self.launch_script + cleanup_script if cleanup else "")
        tf.file.close()

        prelaunch_upload_script, launch = ssh_remote_exec(username, ip, tf.name,
                                                          port=port, pem=pem,
                                                          profile=profile,
                                                          password=password,
                                                          require_password=(profile is not None),
                                                          sudo=sudo)

        # todo: use pipe back to send binary from RPC calls
        if dry:
            if prelaunch_upload_script:
                print("script upload:\n", prelaunch_upload_script)
            print("launch script:\n", launch)
            return

        # note: first pre-upload the script
        if prelaunch_upload_script:
            # done: separate out the two commands
            p = ck(prelaunch_upload_script, verbose=verbose, shell=True, stdout=sys.stdout, stderr=sys.stderr)
            if profile is not None:
                p.communicate(bytes(f"{password}\n"))

        pipe_in = "" if profile is None else f"{password}\n"
        if not prelaunch_upload_script:
            pipe_in = pipe_in + self.launch_script + "\n"

        if verbose:
            print('ssh pipe-in: ')
            print(pipe_in)

        import subprocess
        if block:
            # todo: not supported. stdout, stderr, requires subprocess.PIPE for the two.
            p = subprocess.Popen(launch, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
            return p.communicate(bytes(pipe_in, 'utf-8'))
        elif console_mode:
            p = subprocess.Popen(launch, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        else:
            p = subprocess.Popen(launch, shell=True, stdin=subprocess.PIPE, stdout=sys.stdout, stderr=sys.stderr)

        p.stdin.write(bytes(pipe_in, 'utf-8'))
        p.stdin.flush()

    def launch_ec2(self, image_id, instance_type, key_name, security_group,
                   spot_price=None, iam_instance_profile_arn=None, verbose=False,
                   availability_zone=None,
                   dry=False, name=None, tags={}, **_):
        from termcolor import cprint
        import boto3
        if verbose:
            print('Using the default AWS Profile')

        instance_config = dict(ImageId=image_id, KeyName=key_name, InstanceType=instance_type,
                               SecurityGroups=(security_group,),
                               IamInstanceProfile={'Arn': iam_instance_profile_arn})
        if availability_zone:
            instance_config['Placement'] = dict(AvailabilityZone=availability_zone)

        tags = {snake2camel(k): v for k, v in tags.items()}
        if name:
            tags["Name"] = name
        tag_str = [dict(Key=k, Value=v) for k, v in tags.items()]

        ec2 = boto3.client("ec2")
        if spot_price:
            # for detailed settings see:
            #     http://boto3.readthedocs.io/en/latest/reference/services/ec2.html#EC2.Client.request_spot_instances
            # issue here: https://github.com/boto/boto3/issues/368
            instance_config.update(UserData=base64.b64encode(self.launch_script.encode()).decode("utf-8"))
            response = ec2.request_spot_instances(
                InstanceCount=1, LaunchSpecification=instance_config,
                SpotPrice=str(spot_price), DryRun=dry)
            spot_request_id = response['SpotInstanceRequests'][0]['SpotInstanceRequestId']
            if verbose:
                import yaml
                print(yaml.dump(response))
            if tags:
                ec2.create_tags(DryRun=dry, Resources=[spot_request_id], Tags=tag_str)
            cprint(f'made instance request {spot_request_id}', 'blue')
            return spot_request_id
        else:
            instance_config.update(UserData=self.launch_script)
            response = ec2.run_instances(MaxCount=1, MinCount=1, **instance_config, DryRun=dry)
            instance_id = response['Instances'][0]['InstanceId']
            if verbose:
                print(response)
            if tags:
                ec2.create_tags(DryRun=dry, Resources=[instance_id], Tags=tag_str)
            cprint(f'launched instance {instance_id}', 'green')
            return instance_id

    def launch_gce(self, project_id, zone, instance_type, image_id=None,
                   image_project='deeplearning-platform-release', image_family='pytorch-latest-gpu',
                   accelerator_type=None, accelerator_count=None,
                   preemptible=False,
                   verbose=False, dry=False, name=None, tags={}, **_):
        if verbose:
            print('Using the default GCLoud Profile')

        import googleapiclient.discovery

        compute = googleapiclient.discovery.build('compute', 'v1')

        if image_id is None:
            image_response = compute.images().getFromFamily(project=image_project, family=image_family).execute()
            image_id = image_response['selfLink']

        instance_config = {
            'name': name,
            'machineType': f"zones/{zone}/machineTypes/{instance_type}",
            'scheduling': {
                'preemptable': preemptible,
                # for accelerator enabled instances such as a2-highgpu-*, this needs to be set
                "onHostMaintenance": "terminate",
                "automaticRestart": False
            },

            # Specify the boot disk and the image to use as a source.
            'disks': [
                {
                    'boot': True,
                    'autoDelete': True,
                    'initializeParams': {
                        'sourceImage': image_id,
                    }
                }
            ],

            'automaticRestart': False,

            # Specify a network interface with NAT to access the public
            # internet.
            'networkInterfaces': [{
                'network': 'global/networks/default',
                'accessConfigs': [
                    {'type': 'ONE_TO_ONE_NAT', 'name': 'External NAT'}
                ]
            }],

            # Allow the instance to access cloud storage and logging.
            'serviceAccounts': [{
                'email': 'default',
                'scopes': [
                    'https://www.googleapis.com/auth/devstorage.read_write',
                    'https://www.googleapis.com/auth/logging.write',
                    # for self-termination, full compute read and write
                    'https://www.googleapis.com/auth/compute'
                ]
            }],

            # Metadata is readable from the instance and allows you to
            # pass configuration from deployment scripts to instances.
            'metadata': {
                'items': [
                    dict(key='startup-script', value=self.launch_script),
                    *(dict(key=k, value=v) for k, v in tags.items())
                ]
            },
        }
        if accelerator_type:
            instance_config['accelerator'] = dict(type=accelerator_type, count=accelerator_count)

        if dry:
            return instance_config

        # todo: there is a way to do this in batch mode that is a lot faster.
        instances = compute.instances().insert(
            project=project_id,
            zone=zone,
            body=instance_config
        )
        return instances.execute()

    def manager_host_setup(self, host, verbose=None, **_):
        self.host_unpacked = True
        client = JaynesClient(host)
        host_setup = [dedent(m.host_setup)
                      for m in self.mounts if hasattr(m, "host_setup") and m.host_setup]
        if verbose:
            print(*host_setup, sep="--------")
        pipe_back = client.map(*host_setup)
        if verbose:
            print(pipe_back)
        return pipe_back

    def launch_manager(self, host, launch_dir, script_name="jaynes_launch.sh", project=None, user=None, token=None,
                       sudo=False, cleanup=True, verbose=False, timeout=None, **_):
        from termcolor import cprint

        tf = tempfile.NamedTemporaryFile(prefix="jaynes_launcher-", suffix=".sh", delete=False)
        with open(tf.name, 'w') as f:
            f.write(self.launch_script)
        tf.file.close()

        client = JaynesClient(host)

        remote_script_name = launch_dir + "/" + script_name
        client.upload_file(tf.name, remote_script_name)

        if verbose:
            print(self.launch_script)

        r = client.execute(f"bash {remote_script_name}", timeout)
        try:
            stdout, stderr, error = r
            if stdout:
                print(stdout)
            if error or stderr:
                cprint(stderr, color="red")
        except Exception as e:
            cprint(r, color="red")

    # aliases of launch scripts
    local_docker = launch_local_docker
    ssh = launch_ssh
    ec2 = launch_ec2
    gce = launch_gce
    manager = launch_manager


class RUN:
    count = 0
    config_root = None  # This is the absolute path to the `.jaynes.yml` config file. Used to produce mount paths.
    raw = None
    J: Jaynes = None
    config = None

    # default value for the run mode
    mode = None
    __now = None

    @classmethod
    def now(cls, fmt):
        from datetime import datetime
        if cls.__now is None:
            cls.__now = datetime.now()
        return cls.__now.strftime(fmt)

    @classmethod
    def NOW(cls, fmt):
        from datetime import datetime
        return datetime.now().strftime(fmt)

    @classmethod
    def reset(cls):
        cls.__now = None


def config(mode=None, *, config_path=None, runner=None, host=None, launch=None, **ext):
    """
    Configuration function for Jaynes

    :param mode: the run mode you want to use, specified under the `modes` key inside your jaynes.yml config file
    :param config_path: the path to the configuration file. Allows you to use a custom configuration file
    :param runner: configuration for the runner, overwrites what's in the jaynes.yml file.
    :param host: configuration for the host machine, overwrites what's in the jaynes.yml file.
    :param launch: configuration for the `launch` function, overwrites what's in the jaynes.yml file
    :param ext: variables to pass into the string interpolation. Shows up directly as root-level variables in
                the string interpolation context
    :return: None
    """
    import yaml
    from termcolor import cprint
    from . import mounts, runners
    from datetime import datetime
    from uuid import uuid4

    # RUN.reset()  # do not reset the clock
    RUN.mode = mode
    cprint(f"Launching {mode or '<default>'} mode", color="blue")

    ctx = dict(env=SimpleNamespace(**os.environ), now=datetime.now(), uuid=uuid4(), RUN=RUN, **ext)

    if RUN.J is None:
        if config_path is None:
            for d in cwd_ancestors():
                try:
                    config_path, = glob.glob(d + "/.jaynes.yml")
                    break
                except Exception:
                    pass
        if config_path is None:
            cprint('No `.jaynes.yml` is found. Run `jaynes.init` to create a configuration file.', "red")
            return

        RUN.config_root = os.path.dirname(config_path)

        from inspect import isclass

        # add env class for interpolation
        yaml.SafeLoader.add_constructor("!ENV", hydrate(dict, ctx), )

        for k, c in mounts.__dict__.items():
            if isclass(c):
                yaml.SafeLoader.add_constructor("!mounts." + k, hydrate(c, ctx), )

        for k, c in runners.__dict__.items():
            if hasattr(c, 'from_yaml'):
                yaml.SafeLoader.add_constructor("!runners." + k, c.from_yaml)

        yaml.SafeLoader.add_constructor("!host", hydrate(lambda **args: args, ctx))

        with open(config_path, 'r') as f:
            raw = yaml.safe_load(f)

        # order or precendence: mode -> run -> root
        RUN.raw = raw
        RUN.J = Jaynes()

    RUN.config = RUN.raw.copy()
    if mode == 'local':
        cprint("running local mode", "green")
        return

    elif not mode:
        run = RUN.raw.get('run')
        assert run, "`run` field in .jaynes.yml can not be empty when using default config"
        RUN.config.update(run)
    else:
        modes = RUN.raw.get('modes', {})
        RUN.config.update(modes[mode])

    if runner:
        Runner, runner_config = RUN.config['runner']
        local_copy = runner_config.copy()
        local_copy.update(runner)
        RUN.config['runner'] = Runner, local_copy

    if launch:
        local_copy = RUN.config['launch']
        local_copy.update(launch)
        RUN.config["launch"] = local_copy

    RUN.config.update(ctx)
    RUN.J.set_mount(*RUN.config.get("mounts", []))
    RUN.J.upload_mount(**RUN.config.get('launch', {}), verbose=RUN.config.get('verbose', None))


def run(fn, *args, __run_config=None, **kwargs, ):
    from termcolor import cprint
    from datetime import datetime
    from uuid import uuid4

    if not RUN.J:
        config()

    if RUN.mode == "local":
        return fn(*args, **kwargs)

    # config.RUNNER
    Runner, runner_kwargs = RUN.config.get('runner')
    # interpolation context
    context = RUN.config.copy()
    context['run'] = SimpleNamespace(
        count=RUN.count,
        cwd=os.getcwd(),
        now=datetime.now(),
        uuid=uuid4(),
        pypaths=SimpleNamespace(
            host=":".join([m.host_path for m in RUN.config['mounts'] if m.pypath]),
            container=":".join([m.container_path for m in RUN.config['mounts'] if m.pypath])
        ), **(__run_config or {}))
    RUN.count += 1
    # todo: mapping current work directory correction on the remote instance.

    irunner_kwarg_hydrated = {}
    for k, v in runner_kwargs.items():
        if type(v) is str:
            try:
                irunner_kwarg_hydrated[k] = v.format(**context)
            except IndexError as e:
                a = '\n'
                print(f"{k} '{v}' context: {list(context.items())}")
                raise e
        else:
            irunner_kwarg_hydrated[
                k] = v  # _ = {k: v.format(**context) if type(v) is str else v for k, v in runner_kwargs.items()}
    if 'work_dir' not in irunner_kwarg_hydrated:
        irunner_kwarg_hydrated['work_dir'] = os.getcwd()

    j = RUN.J
    j.set_runner(Runner(**irunner_kwarg_hydrated, mounts=RUN.config.get('mounts', []), ))
    j.runner.run(fn, *args, **kwargs)

    # config.HOST
    launch_config = RUN.config.get('launch', {})

    if launch_config['type'].startswith('ssh') and not j.host_unpacked:

        j.host_unpacked = j.make_host_unpack_script(**launch_config)
        if RUN.config.get('verbose', False):
            print('Unpacking On Remote')
        j.launch_script = j.host_unpacked
        j.ssh(**omit(launch_config, 'type', 'block'), block=True, verbose=RUN.config.get('verbose', False))
        j.launch_script = None

    if launch_config['type'] == "manager" and not j.host_unpacked:
        cprint('unpacking code remotely...', color="green")
        j.manager_host_setup(**launch_config, verbose=RUN.config.get('verbose'))

    j.make_host_script(**launch_config)
    if RUN.config.get('verbose'):
        print(j.launch_script)

    # config.LAUNCH
    kwargs = launch_config.copy()
    kwargs.update(omit(launch_config, 'type'))
    kwargs['verbose'] = RUN.config.get('verbose')

    launch_response = getattr(j, launch_config['type'])(**kwargs)
    if RUN.config.get('verbose'):
        cprint(f"launched! {launch_response}", "green")
    return launch_response


def listen(timeout=None):
    """Just a for-loop, to keep ths process connected to the ssh session"""
    import math, time
    from termcolor import cprint

    cprint('Jaynes pipe-back is now listening...', "blue")

    if timeout:
        time.sleep(timeout)
        cprint(f'jaynes.listen(timeout={timeout}) is now timed out. remote routine is still running.', 'green')
    else:
        while True:
            time.sleep(math.pi * 20)
            cprint('Listening to pipe back...', 'blue')
