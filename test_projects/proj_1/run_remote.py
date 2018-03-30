from jaynes import Jaynes
from main import LOG_DIR, train, abs_path

J = Jaynes(remote_cwd='/home/ubuntu/', bucket="ge-bair", log=LOG_DIR + "/startup.log")
J.mount_s3(local="./", pypath=True)
J.mount_s3(local="../../", pypath=True, file_mask="""./__init__.py ./jaynes""")
J.mount_output(s3_dir=LOG_DIR, local=LOG_DIR, remote=LOG_DIR, docker=abs_path)
J.run_local(verbose=True)
J.setup_docker_run("thanard/matplotlib", docker_startup_scripts=("pip install cloudpickle",), use_gpu=True)
J.make_launch_script(train, a="hey", b=[0, 1, 2], log_dir=LOG_DIR, dry=True, verbose=True)
