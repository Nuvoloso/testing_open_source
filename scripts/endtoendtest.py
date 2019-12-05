#/usr/bin/python3
"""

Copyright 2019 Tad Lebeck
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Deploys a KOPS cluster along with Nuvo Management. Upon a successful completion, it stores
the hostname of Nuvo Kontroller in ./nuvomgmthostname.txt

Dependencies:
It needs the Nuvo Kontroller hostname as input. To deploy one, use deploy_nuvo_kontroller.py
PYTHONPATH needs to be set in the environment (export PYTHONPATH=/home/ubuntu/testing/lib)


Example of a run command:
python3 testing/scripts/endtoendtest.py --kops_cluster_name nuvodataplane.k8s.local \
 --nodes 3 --kops_state_store s3://kops-neha-nuvoloso --aws_access_key AKIAJXXXXMQ \
--aws_secret_access_key yISSSSSSSS6xbhCg/ --region us-west-2 --nuvo_kontroller_hostname \
ac7862895022711e9b5fe0acfcc322df-2138322790.us-west-2.elb.amazonaws.com --incremental_xfers 2

This test succeeds only when the comparison of text files on original Nuvo volume and
restored Nuvo volume match. It can exit before reaching this point with an error/exception.

Detailed steps:
1. Reads input params
2. Installs kops, kubectl, awscli
3. Configures aws. This step is skipped if '.aws/credentials' exists. Otherwise, it creates 'config'
and 'credentials' files under '.aws' directory.
4. Generates ssh-keypair unless '/.ssh/id_rsa' already exists
5. Runs 'kops create cluster' command without '--yes' option to check setup is valid.
6. Runs 'kops update cluster' (and 'kops rolling-update cluster', if needed) with '--yes'
and loops/waits
till dataplane (aka nuvo) cluster is ready.
7. Calls Nuvo Management APIs to:
    - create a CSP domain, then,
    - deploy nuvo cluster namespace (i.e. '-n nuvolsolo-cluster'),
    - does Service Plan Allocation,
    - create a nuvo volume object,
    - launch pvc for that nuvo volume
8. Now, it is ready to deploy a user's app container that will use the nuvo-volume. For this, it:
    - reads the template of the user-app's yaml file (testing/config/shell.yaml),
    - copies the template file into a new yaml file (under 'logs' directory) and
    - adds the pvc name (obtained in step #7) to it. Then,
    - it launches this newly created yaml file and waits for it to go in 'Running state. It also
    - verifies that volume's uuid and the pvc match the output of 'kubectl get pv'
9. Test then runs IO to the nuvo volume. For baseline transfer, test creates a directory and a
few small files. It does this by 'echoing' into a string into a shell script (app_io_script.sh)
Then, copies this shell script into the user-container and runs it inside the container.
10. Calls Nuvo APIs to do a backup and restore.
11. Gets the PVC of the restored volume and launches it.
12. Deploys a new user-container with the restored volume's pvc.
13. Verifies data by comparing the MD5 checksum of all the tiny files created on source and
destination. If the command ('kubectl exec md5sum') doesn't yield output or result doesn't match
(for any of the files), test throws an exception.
14. Now, we do the incremental transfers (default = 1). For this, first it runs IO again. This time
(baseline=False), it runs 'fio' to create a number of files (/datadir/dir1/incremental*) on the
nuvovolume on top of FileSystem)
15. Calls Nuvo API to run backup and restore
16. Gets the PVC of the restored volume (of incremental transfer) and launches it.
17. Deploys a new user-container with this newly restored volume.
18. Verifies MD5 checksums of all the files (3 tiny files from baseline, and new ones - from
incremental transfer)
19. Repeats steps 14 - 18 for rest of the incremental transfers. Each time, the 'fio' files are
overwritten on the source (to create new IO for backup). Remember, 'fio' file is on top of
FileSystem (not raw blocks).
20. Exits successully when all args.incremental_xfers are done.
"""

import argparse
import datetime
import logging
import os
import pathlib
import time
import subprocess
import re
import uuid
from ruamel.yaml import YAML

from nuvoloso.dependencies.install_packages import InstallPackages
from nuvoloso.dependencies.kops_cluster import KopsCluster
from nuvoloso.api.nuvo_management import NuvoManagement
from nuvoloso.dependencies.kubectl_helper import KubectlHelper
from nuvoloso.nuvoloso.nuvoloso_helper import NuvolosoHelper
from nuvoloso.api.obj_helper import VolumeSeriesRequestObject, VolumeSeriesObject

CHECK_OUTPUT_TIMEOUT = 600
CHECK_OUTPUT_FIO_TIMEOUT = 3600
DEFAULT_CENTRALD_CLUSTER_NAME = "NuvoCentraldTestAuto.k8s.local"
PREFIX_STR = "restore-base-"
PREFIX_STR_INC = "restore-inc-"
USERAPP_FILENAME = "shell.yaml"
USERAPP_RESTORED_FILENAME = "shellrestored.yaml"
WAIT_TIMEOUT = 3600
NUVO_CONTAINERS = ["agentd", "nuvo"]
DEFAULT_NAMESPACE = "e2e"

# service plan names
SP_DSS_DATA = "DSS"
SP_GENERAL = "General"
SP_GENERAL_PREMIER = "General Premier"
SP_OLTP_DATA = "OLTP"
SP_OLTP_DATA_PREMIER = "OLTP Premier"
SP_ONLINE_ARCHIVE = "Online Archive"
SP_STREAMING_ANALYTICS = "Streaming Analytics"
SP_TECHNICAL_APPLICATIONS = "Technical Applications"

# use bionic version of ubuntu
DEFAULT_AMI = '099720109477/ubuntu/images/hvm-ssd/ubuntu-bionic-18.04-amd64-server-20190212.1'

FIO_FILE_NAME_BASE = 'incremental_test'
TEST_DIR = "/dir1"
DATA_DIR = "/datadir"

class TestBackupRestoreBasic(object):
    '''Deploys centrald and user-app containers  in different namespaces, of a new KOPS cluster.
    Writes few files/directories on a Nuvo volume and backs it. Then deploys a new user-app
    container and mounts the restored volume and verifies the data is same.'''

    def __init__(self, args):
        self.args = args
        self.nuvo_mgmt = NuvoManagement(args)
        self.kubectl_helper = KubectlHelper(args, args.kops_cluster_name)
        self.nuvoloso_helper = NuvolosoHelper(args)
        self.nuvo_mgmt.update_snapshot_policy_account()
        self.md5_files = {}
        self.file_paths = [TEST_DIR + "/file1.txt",
                           TEST_DIR + "/file2.txt",
                           TEST_DIR + "/file3.txt"]
        self.namespace = DEFAULT_NAMESPACE

    def collect_container_status(self):
        '''Container status of app and nuvo clusters'''

        result = self.kubectl_helper.describe_pods(namespace=self.namespace)
        if result:
            logging.info(result)
        result = self.kubectl_helper.get_pods(namespace='nuvoloso-cluster')
        if result:
            logging.info(result)
        result = self.kubectl_helper.get_pv()
        if result:
            logging.info(result)
        logging.info("Failed to deploy app cluster/namespace")

    def deploy_user_app(self, volume_name, pvc_name):
        '''Deploys a container for user-app with Nuvo volume'''

        # Edit the yaml file to add nuvomatcher id
        # Copy original app yaml file into a new one with nuvo-matcher string.
        app_orig_yaml_path = self.args.config_dirpath + "/" + USERAPP_FILENAME
        app_yaml_path = self.args.log_dirpath + "/" + USERAPP_FILENAME.split(".")[0] + "-" + \
            str(uuid.uuid4())[:5] + ".yaml"
        yaml = YAML()
        with open(app_orig_yaml_path, "r") as f_read, open(app_yaml_path, 'w') as f_write:
            data = yaml.load(f_read)
            data['metadata']['name'] = "%s-init" % self.args.pod_prefix
            data['metadata']['namespace'] = DEFAULT_NAMESPACE
            data['metadata']['labels']['name'] = "%s-init" % self.args.pod_prefix
            data['spec']['containers'][0]['name'] = "%s-init" % self.args.pod_prefix 
            data['spec']['volumes'][0]['persistentVolumeClaim']['claimName'] = pvc_name
            f_write.write(yaml.dump(data))

        self.init_pod_name = "%s-init" % self.args.pod_prefix
        self.init_container_name = "%s-init" % self.args.pod_prefix

        logging.info("Deploy user-app yaml file: %s to create a "
                     "container using nuvo-vol volume: %s ", app_yaml_path, volume_name)
        try:
            result = self.kubectl_helper.deploy(app_yaml_path)
            if result:
                logging.info(result)
            if self._wait_for_app_container(count=1):
                result = self.kubectl_helper.get_pv()
                if result:
                    logging.info(result)
                if self.args.dynamic:
                    if pvc_name in result and volume_name in result and (result.count(
                            "Bound") == 1):
                        logging.info("SUCCESS - nuvo-volume with pvc: %s mounted fine "
                                     " inside app container", pvc_name)
                elif self.nuvo_mgmt.get_volume_id(
                        volume_name) in result and pvc_name in result and (result.count(
                            "Bound") == 1):
                    logging.info("SUCCESS - nuvo-volume with pvc: %s mounted fine "
                                 " inside app container", pvc_name)
                else:
                    raise Exception("nuvo-volume's matcher id not found in result")
            else:
                raise Exception("Failed to deploy app container")
        except subprocess.CalledProcessError as err:
            if err.output:
                logging.error(err.output)
            self.collect_container_status()
            raise
        except:
            self.collect_container_status()
            raise

    def _fio_file_name(self):
        return DATA_DIR + TEST_DIR + "/" + FIO_FILE_NAME_BASE

    def _sp_fio_cmd(self, service_plan):
        # pylint: disable=fixme, line-too-long
        sp_to_fio = {
            SP_DSS_DATA: '--rw=write --rwmixread=10 --bs=8k --direct=1 --size=10g --numjobs=1',
            SP_GENERAL: '--rw=randrw --rwmixread=70 --bs=4k --direct=1 --size=4g --numjobs=1 --rate=400k',
            SP_GENERAL_PREMIER: '--rw=randrw --rwmixread=70 --bssplit=512/10:1k/5:2k/5:4k/60:8k/2:16k/4:32k/4:64k/10 --direct=1 --size=4g --numjobs=4',
            SP_OLTP_DATA: '--rw=randrw --rwmixread=50 --bs=8k --direct=1 --size=4g --numjobs=4',
            SP_OLTP_DATA_PREMIER: '--rw=randrw --rwmixread=70 --bs=8k --direct=1 --size=4g --numjobs=4',
            SP_ONLINE_ARCHIVE: '--rw=write --bs=128k --direct=1 --size=10g --numjobs=1',
            SP_STREAMING_ANALYTICS: '--rw=write --rwmixread=10 --bs=128k --direct=1 --size=10g --numjobs=1',
            SP_TECHNICAL_APPLICATIONS: '--rw=randrw --rwmixread=70 --bssplit=512/10:1k/5:2k/5:4k/60:8k/2:16k/4:32k/4:64k/10 --direct=1 --size=4g --numjobs=4'
        }
        fio_for_sp = sp_to_fio.get(service_plan, 'Unknown')

        full_job_str = 'fio --name=' + self._fio_file_name() + ' ' + fio_for_sp + ' ' + \
            '--fadvise_hint=0 --ioengine=libaio --time_based  --iodepth=16 --eta=always ' + \
            '--runtime=' + self.args.runtime

        return full_job_str
        # pylint: enable=fixme, line-too-long

    def _flush_to_disk(self):
        # Do a sync to force a flush

        sync_string = "\"sync\""
        cmd_sync_script = "echo " + sync_string + " > sync_script.sh"

        try:
            result = self.nuvoloso_helper.run_check_output(cmd_sync_script)
            if result:
                logging.info(result)
            result = self.nuvoloso_helper.run_check_output("chmod +x sync_script.sh")
            if result:
                logging.info(result)
            result = self.kubectl_helper.copy_file_to_cluster("sync_script.sh",
                                                              "/sync_script.sh",
                                                              self.init_pod_name,
                                                              container_name=self.init_container_name,
                                                              namespace = self.namespace)
            if result:
                logging.info(result)
            result = self.kubectl_helper.run_kubectl_exec_cmd("bash sync_script.sh", self.init_pod_name, self.init_container_name, namespace=self.namespace)
            if result:
                logging.info(result)
        except subprocess.CalledProcessError as err:
            if err.output:
                logging.info(err.output)
            logging.info("Failed to run cmds 'sync' inside app container")
            raise

    def run_io_vol(self, iteration, baseline=True):
        '''Run IO  against the nuvo volume, inside container'''

        backup_dir = "/datadir"

        app_string = \
            "\"df -Th; ls -lrt ; pwd; mkdir /datadir/dir1 && echo " + \
            "'\"HELLO HELLO HELLO\"' > " + backup_dir + self.file_paths[0] + "; echo " + \
            "'\"WORLD WORLD WORLD\"' > " + backup_dir + self.file_paths[1] + "; echo " + \
            "'\"BYE BYE\"' > " + backup_dir + self.file_paths[2] +  " \""

        try:
            if baseline:
                result = self.nuvoloso_helper.run_check_output("echo " + app_string + " > app_io_script.sh")
                if result:
                    logging.info(result)
                result = self.nuvoloso_helper.run_check_output("chmod +x app_io_script.sh")
                if result:
                    logging.info(result)
                result = self.kubectl_helper.copy_file_to_cluster("app_io_script.sh", "/app_io_script.sh", self.init_pod_name, container_name=self.init_container_name, namespace=self.namespace)
                if result:
                    logging.info(result)
                result = self.kubectl_helper.run_kubectl_exec_cmd("ls", self.init_pod_name, self.init_container_name, namespace=self.namespace)
                if result:
                    logging.info(result)
                result = self.kubectl_helper.run_kubectl_exec_cmd("bash app_io_script.sh", self.init_pod_name, self.init_container_name, namespace=self.namespace)
                if result:
                    logging.info(result)
                result = self.kubectl_helper.run_kubectl_exec_cmd("ls -lrt /datadir/dir1", self.init_pod_name, self.init_container_name, namespace=self.namespace)
                for i, file_path in enumerate(self.file_paths):
                    logging.info('Evaluating file # %s', i)
                    result_backup_file = self.kubectl_helper.run_kubectl_exec_cmd("cat " + backup_dir + file_path, self.init_pod_name, self.init_container_name, namespace=self.namespace)
                    logging.info(result_backup_file)
                    result_md5_orig_file = self.kubectl_helper.run_kubectl_exec_cmd(" md5sum " + backup_dir + file_path, self.init_pod_name, self.init_container_name, namespace=self.namespace)
                    if not result_md5_orig_file:
                        raise Exception("cmd: %s returned nothing" % " md5sum " + backup_dir + file_path)
                    # Store only the MD5s, not the file paths
                    self.md5_files[file_path] = result_md5_orig_file.split(" ")[0]
            else:
                kubectl_fio_cmd = self._sp_fio_cmd(self.args.serviceplan_name)

                install_cmd = 'bash -c "apt-get update && apt-get install fio -y"'
                logging.info("kubectl_fio_install_cmd: %s", install_cmd)
                result_fio_install_out = self.kubectl_helper.run_kubectl_exec_cmd(install_cmd, self.init_pod_name, self.init_container_name, namespace=self.namespace)
                if result_fio_install_out:
                    logging.info(result_fio_install_out)

                logging.info("kubectl_fio_cmd: %s", kubectl_fio_cmd)
                result_fio_file_out = self.kubectl_helper.run_kubectl_exec_cmd(kubectl_fio_cmd, self.init_pod_name, self.init_container_name, namespace=self.namespace)
                if result_fio_file_out:
                    logging.info(result_fio_file_out)

                self._flush_to_disk()
                #
                # Need to get md5 checksums for all FIO files that were created
                #
                fio_file_list_cmd = 'bash -c "ls ' + \
                    '/datadir' + TEST_DIR + '/' + FIO_FILE_NAME_BASE + '*"'
                logging.info("fio_file_list_cmd: %s", fio_file_list_cmd)
                list_output = self.kubectl_helper.run_kubectl_exec_cmd(fio_file_list_cmd, self.init_pod_name, self.init_container_name, namespace=self.namespace)
                fio_files = list_output.strip().split()

                for i, fio_file in enumerate(fio_files):
                    md5_orig_file_cmd = 'bash -c "md5sum ' + fio_file + '"'
                    logging.info("md5_orig_file_cmd: %s", md5_orig_file_cmd)
                    result_md5_orig_file = self.kubectl_helper.run_kubectl_exec_cmd(md5_orig_file_cmd, self.init_pod_name, self.init_container_name, namespace=self.namespace)

                    if not result_md5_orig_file:
                        raise Exception("cmd: %s returned nothing" % md5_orig_file_cmd)

                    result_md5_orig = result_md5_orig_file.split(" ")[0]
                    subdir_name = fio_file[8:]
                    self.md5_files[subdir_name] = result_md5_orig
                    if iteration == 1:
                        self.file_paths.append(subdir_name)

            # run sync inside the container
            self._flush_to_disk()
            logging.info("MD5 checksums of files on source: %s", self.md5_files)
        except subprocess.CalledProcessError as err:
            if err.output:
                logging.error(err.output)
            logging.error("Failed to run cmds inside app container")
            raise

    def _wait_for_app_container(self, count=1):
        time_now = datetime.datetime.utcnow()
        cmd_succeeded = False
        while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(seconds=WAIT_TIMEOUT)):
            time.sleep(60)
            running_pods = 0
            result = self.kubectl_helper.get_pods(namespace=self.namespace)
            if result:
                logging.info(result)
                for pod in result.splitlines():
                    if self.args.pod_prefix == pod.split()[0].split('-')[0] and pod.split()[2] == "Running":
                        running_pods += 1
                
                if running_pods == count:
                    logging.info("Pod(s) in app cluster ('default') are running fine")
                    cmd_succeeded = True
                    time.sleep(20)
                    break
                else:
                    logging.info("Pod(s) (default namespace) are not ready yet. Running pods expected: %s Found: %s"
                                 "Sleeping for 60 seconds..", count, running_pods)
            else:
                raise Exception("Failed while deploying the application. Get pods"
                                " did not yield any response")
        if not cmd_succeeded:
            raise Exception("TIMEOUT (%s seconds) waiting for pods to"
                            "  go to 'Running' state." % WAIT_TIMEOUT)
        return cmd_succeeded

    def deploy_app_using_restored_vol(self, restored_volume_name, restored_pvc_name,
                                      restore_iteration=1):
        '''Deploy another user-app container and mount restored volume'''

        # Edit the yaml file (of restored vol) to add nuvomatcher id
        # Copy original app yaml file into a new one with nuvo-matcher string.
        app_orig_yaml_path = self.args.config_dirpath + "/" + USERAPP_RESTORED_FILENAME
        restored_app_yaml_path = \
            self.args.log_dirpath + "/" + USERAPP_RESTORED_FILENAME.split(".")[0] + \
                "-" + str(restore_iteration) + "-" + str(uuid.uuid4())[:5] + ".yaml"

        yaml = YAML()
        with open(app_orig_yaml_path, "r") as f_read, open(restored_app_yaml_path, 'w') as f_write:
            data = yaml.load(f_read)
            data['metadata']['namespace'] = DEFAULT_NAMESPACE
            data['metadata']['name'] = "%s-restored-%s" % (self.args.pod_prefix, str(restore_iteration))
            data['metadata']['labels']['name'] = "%s-restored-%s" % (self.args.pod_prefix, str(restore_iteration))
            data['spec']['containers'][0]['name'] = "%s-restored-%s" % (self.args.pod_prefix, str(restore_iteration))
            data['spec']['volumes'][0]['persistentVolumeClaim']['claimName'] = restored_pvc_name
            f_write.write(yaml.dump(data))

        self.restored_pod_prefix = "%s-restored-" % (self.args.pod_prefix)
        self.restored_container_prefix = "%s-restored-" % (self.args.pod_prefix)
         
        logging.info("Deploy user-app yaml file: %s to create a container using RESTORED "
                     "nuvo-vol volume: %s ", restored_app_yaml_path, restored_volume_name)
        try:
            result = self.kubectl_helper.deploy(restored_app_yaml_path)
            if result:
                logging.info(result)
            # Total pods = number of restores + 1 (original source pod)
            if self._wait_for_app_container(count=restore_iteration + 1):
                result = self.kubectl_helper.get_pv()
                if result:
                    logging.info(result)

                # Number of pvs is 1 more than number of restores due to pv of the
                # original baseline restored volume
                if result.find(self.nuvo_mgmt.get_volume_id(restored_volume_name)):
                    logging.info("SUCCESS - RESTORED nuvo-volume with matcher_ud: %s mounted fine "
                                 "inside NEW app container", restored_pvc_name)
                else:
                    raise Exception("RESTORED nuvo-volume's matcher id not found in result")
            else:
                raise Exception("Failed to deploy app container with restored volume")
        except subprocess.CalledProcessError as err:
            if err.output:
                logging.error(err.output)
            self.collect_container_status()
            raise
        except:
            self.collect_container_status()
            raise

    def verify_data_src_dst(self, restore_iteration=1):
        '''Run IO  against the nuvo volume, inside container'''

        kubectl_cmd_run = "bash app_io_restore_script.sh"
        kubectl_md5_sum = "md5sum /datadirrestored"

        try:
            result = self.nuvoloso_helper.run_check_output(
                "echo " +
                "\"df -Th; ls -lrt /datadirrestored; ls -lrt /datadirrestored/dir1\"" +
                "> app_io_restore_script.sh")
            if result:
                logging.info(result)
            result = self.nuvoloso_helper.run_check_output("chmod +x app_io_restore_script.sh")
            if result:
                logging.info(result)

            pod_name = self.restored_pod_prefix + str(restore_iteration)
            container_name = self.restored_container_prefix  + str(restore_iteration)

            result = self.kubectl_helper.copy_file_to_cluster("app_io_restore_script.sh",
                                                              "/app_io_restore_script.sh",
                                                              pod_name,
                                                              container_name,
                                                              namespace=self.namespace)
            if result:
                logging.info(result)

            result = self.kubectl_helper.run_kubectl_exec_cmd("ls", pod_name, container_name, namespace=self.namespace)
            if result:
                logging.info(result)

            result = self.kubectl_helper.run_kubectl_exec_cmd(kubectl_cmd_run, pod_name, container_name, namespace=self.namespace)
            if result:
                logging.info(result)

            for i, file_path in enumerate(self.file_paths):
                logging.info("checksum to compare for file # %s %s is: %s", i, file_path, self.md5_files[file_path])
                compare_file_name = '/datadirrestored' + file_path
                kubectl_md5_sum = 'bash -c "md5sum ' + compare_file_name + '"'
                logging.info("kubectl_md5_sum: %s", kubectl_md5_sum)
                result_md5_restored_file = self.kubectl_helper.run_kubectl_exec_cmd(kubectl_md5_sum, pod_name, container_name, namespace=self.namespace)
                logging.info("result_md5_restored_file: %s", result_md5_restored_file)

                md5_restored_file = result_md5_restored_file.split()[0]

                if md5_restored_file != self.md5_files[file_path]:
                    logging.error("ERROR - checksums do not match")
                    return False

            logging.info("SUCCESS - Backup and restored file have same data")
            return True

        except subprocess.CalledProcessError as err:
            if err.output:
                logging.error(err.output)
            logging.error("Failed to run cmds inside app container")
            raise

    def cleanup(self):
        '''Cleanup'''
        KopsCluster.kops_delete_cluster(kops_cluster_name=self.args.kops_cluster_name,
                                        kops_state_store=self.args.kops_state_store)
        domain_id = self.nuvo_mgmt.get_csp_domain_id_from_cluster(self.args.nuvo_cluster_name)
        self.nuvo_mgmt.wait_for_cluster_state(self.args.nuvo_cluster_name, "TIMED_OUT", domain_id=domain_id)
        self.kubectl_helper.cleanup_cluster(self.args.nuvo_cluster_name, \
            self.nuvo_mgmt.get_csp_domain(domain_id)[0]['name'] , self.args.tenant_admin)
        

    def install_dependencies(self):
        '''Install dependencies'''
        InstallPackages.apt_get_update()
        InstallPackages.install_kops()
        InstallPackages.install_kubectl()
        InstallPackages.install_awscli()
        InstallPackages.configure_aws(self.args)
        InstallPackages.generate_sshkeypair()
        return

    def create_application_cluster(self):
        '''Create application cluster'''
        KopsCluster.create_kops_app_cluster(self.args)
        try:
            self.nuvo_mgmt.switch_accounts(self.args.tenant_admin)
            csp_domain_id = self.nuvo_mgmt.create_csp_domain()
            nuvo_cluster_name = self.nuvo_mgmt.deploy_clusterd(csp_domain_id)
            logging.info("nuvo_cluster_name: %s", nuvo_cluster_name)
            self.nuvo_mgmt.do_service_plan_allocation(nuvo_cluster_name, self.args.account_name)
            snapshot_catalog_pd = self.nuvo_mgmt.create_protection_domain()
            self.nuvo_mgmt.set_snapshot_catalog_policy(snapshot_catalog_pd, csp_domain_id)
            self.nuvo_mgmt.switch_accounts(self.args.account_name)
            protection_domain_id = self.nuvo_mgmt.create_protection_domain()
            self.nuvo_mgmt.set_protection_domain(protection_domain_id, csp_domain_id)
        except subprocess.CalledProcessError as err:
            if err.output:
                logging.info(err.output)
            raise
        return nuvo_cluster_name

    def end_to_end(self):
        '''Run end to end test-steps'''

        try:
            nuvo_cluster_name = self.args.nuvo_cluster_name
            self.nuvo_mgmt.switch_accounts(self.args.tenant_admin)
            spa_id = self.nuvo_mgmt.do_service_plan_allocation(nuvo_cluster_name, self.args.account_name)
            self.nuvo_mgmt.switch_accounts(self.args.account_name)

            self.kubectl_helper.create_namespace(self.namespace)
            self.nuvo_mgmt.launch_secret_yaml(nuvo_cluster_name, namespace=self.namespace)
            volume_list = []

            if self.args.dynamic:
                # volume name will be dynamic
                # need to get YAML from pool
                pvc_name_prefix = "nuvoauto-pvc-"
                pvc_name = pvc_name_prefix + str(uuid.uuid4())[:5]
                # launch volume in k8s
                volume_name = self.nuvo_mgmt.launch_volume_pvc_dynamic(pvc_name,
                                                                       spa_id,
                                                                       vol_size=self.args.vol_size,
                                                                       namespace=self.namespace)
            else:
                volume_name = self.nuvo_mgmt.create_volume(nuvo_cluster_name,
                                                           vol_size=self.args.vol_size)
                # launch volume in k8s
                pvc_name = self.nuvo_mgmt.launch_volume_pvc(volume_name, namespace=self.namespace)
            
            volume_list.append(volume_name)
            # write
            self.deploy_user_app(volume_name, pvc_name)
            self.run_io_vol(iteration=0, baseline=True)

            # backup & restore
            restored_vol_name = self.nuvo_mgmt.backup_restore_volume(volume_name,
                                                                     nuvo_cluster_name,
                                                                     prefix_str=PREFIX_STR)
            restored_pvc_name = self.nuvo_mgmt.launch_volume_pvc(restored_vol_name, namespace=self.namespace)

            # deploy app container
            self.deploy_app_using_restored_vol(restored_vol_name,
                                               restored_pvc_name,
                                               restore_iteration=1)

            # verify
            success = self.verify_data_src_dst(restore_iteration=1)

            for i in range(1, self.args.incremental_xfers + 1):
                logging.info("Starting incremental backup and restores..")
                self.nuvo_mgmt.authorize()

                # Write a new file for incremental backup
                self.run_io_vol(baseline=False, iteration=i)

                # Run backup and restore again
                restored_vol_name = self.nuvo_mgmt.backup_restore_volume(volume_name,
                                                                         nuvo_cluster_name,
                                                                         prefix_str=PREFIX_STR_INC +
                                                                         str(i) + "-")

                restored_pvc_name = \
                    self.nuvo_mgmt.launch_volume_pvc(restored_vol_name, namespace=self.namespace)
                self.deploy_app_using_restored_vol(PREFIX_STR_INC + str(i) + "-" + volume_name,
                                                   restored_pvc_name,
                                                   restore_iteration=i + 1)
                volume_list.append(restored_vol_name)


                # verify
                success = self.verify_data_src_dst(restore_iteration=i + 1)
                logging.info("Completed iteration: %d of total: %d of Incremental "
                             "backup and restores.", i, self.args.incremental_xfers)

            result = self.kubectl_helper.get_pods(namespace=self.namespace)
            if result:
                logging.info(result)
            for pod in result.splitlines():
                if self.args.pod_prefix == pod.split()[0].split('-')[0]:
                    self.kubectl_helper.delete_pod(pod.split()[0], namespace=self.namespace)
            for volume in volume_list:
                self.nuvo_mgmt.backup_consistencygroup(nuvo_cluster_name, volume_name)

            # cleanup
            if success:
                if self.args.do_cleanup:
                    logging.info("Test succeeded. Deleting kops cluster now")
                    self.cleanup()
                else:
                    logging.info("Test succeeded. All pods deleted. Skipping kops delete cluster since "
                                 "do_cleanup is False")
            else:
                logging.error("Test FAILED. Skipping kops delete cluster")
                raise Exception("Test Failed..Skipping cleanup")
        except subprocess.CalledProcessError as err:
            if err.output:
                logging.error(err.output)
            raise
        except:
            logging.error('Unexpected exception in end to end test')
            raise

def main():
    '''main'''

    default_cluster_name = DEFAULT_CENTRALD_CLUSTER_NAME
    default_aws_access_key = default_aws_secret_access_key = default_kops_state_store = None

    if os.environ.get('KOPS_CLUSTER_NAME'):
        default_cluster_name = os.environ.get('KOPS_CLUSTER_NAME')

    if os.environ.get('KOPS_STATE_STORE') is None:
        default_kops_state_store = os.environ.get('KOPS_STATE_STORE')

    if os.environ.get('AWS_ACCESS_KEY'):
        default_aws_access_key = os.environ.get('AWS_ACCESS_KEY')

    if os.environ.get('AWS_SECRET_ACCESS_KEY') is None:
        default_aws_secret_access_key = os.environ.get('AWS_SECRET_ACCESS_KEY')

    parser = argparse.ArgumentParser(description="Deploys a kops cluster with "
                                     "Nuvo data plane")
    parser.add_argument(
        '--kops_cluster_name', help='name of kops cluster for Nuvo Data Plane[default: ' +
        default_cluster_name + ']',
        default=default_cluster_name)
    parser.add_argument(
        '--nodes', help='number of nodes in the cluster [default=1]', type=int, default=1,
        choices=range(1, 101))
    parser.add_argument(
        '--kops_state_store', help='state store for cluster',
        default=default_kops_state_store)
    parser.add_argument(
        '--aws_access_key', help='aws AccessKey', default=default_aws_access_key)
    parser.add_argument(
        '--aws_secret_access_key', help='aws SecretAccessKey',
        default=default_aws_secret_access_key)
    parser.add_argument(
        '--region', help='aws region', default=None)
    parser.add_argument(
        '--k8s_master_zone', help='aws zone for master node', default=None)
    parser.add_argument(
        '--k8s_nodes_zone', help='aws zone for other nodes [default=us-west-2c]', default=None)
    parser.add_argument(
        '--master_size', help='ec2 instance type for master node [t2.large]', default=None)
    parser.add_argument(
        '--node_size', help='ec2 instance type for other nodes [t2.large]', default=None)
    parser.add_argument(
        '--nuvo_kontroller_hostname', help='Hostname of https svc of Nuvo Kontroller',
        required=True)
    parser.add_argument(
        '--config_dirpath', help='directory path to user-app yaml files.')
    parser.add_argument(
        '--vol_size', help='Volume size in GiB [default=20]', type=int, default=20,
        choices=range(1, 100))
    parser.add_argument(
        '--incremental_xfers', help='Run incremental backup and restores [default=3]',
        type=int, default=3, choices=range(0, 101))
    parser.add_argument(
        '--account_name', help='Nuvoloso account name', default='Normal Account')
    parser.add_argument(
        '--tenant_admin', help='Tenant admin account name for doing service plan allocations', default='Demo Tenant')
    parser.add_argument(
        '--log_dirpath', help='log dir to hold test and nuvo logs', default=None)
    parser.add_argument(
        '--do_cleanup', help='Delete kops cluster of data plane in the end',
        action='store_true', default=False)
    parser.add_argument(
        '--kubernetes_version', help='version of kubernetes to deploy', default=None)
    parser.add_argument(
        '--image', help='AMI Image for all instances', default=DEFAULT_AMI)
    parser.add_argument(
        '--node_volume_size', help='volume size for slave nodes of k8s cluster', type=int,
        default=10)
    parser.add_argument(
        '--master_volume_size', help='volume size for master node of k8s cluster', type=int,
        default=20)
    parser.add_argument(
        '--serviceplan_name', help='Name of service plan [default=General]', default="General")
    parser.add_argument(
        '--dynamic', help='Use dynamic provisioning',
        action='store_true', default=True)
    parser.add_argument(
        '--nuvo_cluster_name', help='Nuvo cluster name already created. \
                Passing this argument assumes that the application cluster is already created',
                default=None)
    parser.add_argument(
        '--runtime', help='Runtime for FIO job', default='600')
    parser.add_argument(
        '--pod_prefix', help='A prefix for all pod names', default='e2e')

    args = parser.parse_args()
    assert(args.kops_cluster_name and args.region and args.kops_state_store and args.aws_access_key
           and args.aws_secret_access_key), "Some/all input parameters are not filled. Aborting"
    home_dir = str(pathlib.Path.home())
    if not args.config_dirpath:
        args.config_dirpath = str(pathlib.Path(__file__).parent.absolute()/'../config/')
    args.log_dirpath = args.log_dirpath if args.log_dirpath else home_dir + "/logs-" + \
            str(uuid.uuid4())[:5]
    pathlib.Path(args.log_dirpath).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(format='%(asctime)s %(message)s', filename=args.log_dirpath + "/" +
                        os.path.basename(__file__) + ".log", level=logging.INFO)

    print("Script to Run backup and restore of a Nuvo volume")
    test = TestBackupRestoreBasic(args)
    if not args.nuvo_cluster_name:
        logging.info("Creating Application cluster")
        test.install_dependencies()
        args.nuvo_cluster_name = test.create_application_cluster()
    test.end_to_end()

if __name__ == '__main__':
    main()
