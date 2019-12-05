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

Test to verify whether a change in protection domain id does not affect subsequent snapshot creations.
After a protection domain change, the next snapshot creation should be a 'full' snapshot and not an incremental one.

Prerequisites :
1. Requires a kontroller hostname. A K8s cluster with the kontroller software should be deployed.
To deploy one, use deploy_nuvo_kontroller.py.
2. PYTHONPATH needs to be set in the environment (export PYTHONPATH=/home/ubuntu/testing/lib)
3. Optionally, application cluster(s) can be deployed as well using deploy_app_cluster.py

Example:

PYTHONPATH=~/testing/lib/ python3 ~/testing/scripts/pd_change_snapshot_test.py --app_cluster appcluster.k8s.local --nodes 1 --kops_state_store s3://nri-test-bucket
 --aws_access_key XXXXX --aws_secret_access_key XXXXXXXXXXXXX --nuvo_kontroller_hostname ac79fe4bbd5a211e98ab40ac475462be-1424111265.us-west-2.elb.amazonaws.com 

To use an existing app cluster, use the --nuvo_cluster to pass the cluster name created in the kontroller

PYTHONPATH=~/testing/lib/ python3 ~/testing/scripts/pd_change_snapshot_test.py --app_cluster appcluster.k8s.local --nodes 1 --kops_state_store s3://nri-test-bucket
 --aws_access_key XXXXX --aws_secret_access_key XXXXXXXXXXXXX --nuvo_kontroller_hostname ac79fe4bbd5a211e98ab40ac475462be-1424111265.us-west-2.elb.amazonaws.com 
 --nuvo_cluster nuvoauto-53057


Check help for other optional parameters.

Test Steps:
1. Deploy pod(s) running fio writes with a dynamically provisioned volume.
2. Once all pods are successful, delete them to trigger snapshot creation.
3. Verify that protection domain id on the snapshot object matches the one initially set.
4. Bind/Publish volume to the same cluster so that the same volume can be re-used.
5. Change the protection domain id for the csp domain.
6. Deploy fio jobs doing read verify with the same volume.
7. Once all pods are successful, delete them to trigger snapshot creation again.
8. Verify that the latest snapshot's protection domain id is same the new one set in step 5.
"""

import argparse
import collections
import datetime
import logging
import os
import pathlib
import time
import subprocess
import uuid
import yaml
import json

from nuvoloso.dependencies.install_packages import InstallPackages
from nuvoloso.dependencies.kops_cluster import KopsCluster
from nuvoloso.dependencies.kubectl_helper import KubectlHelper
from nuvoloso.api.nuvo_management import NuvoManagement
from nuvoloso.nuvoloso.connect_ssh import ConnectSSH

FIO_MOUNTPATH = "/datadir"
FIO_MOUNTNAME = "datavol"
TEST_RUNTIME_DELTA = 10800
APP_YAML_TEMPLATE = "fioverify.yaml"
WRITE_JOB = "write_verify.fio"
READ_JOB = "read_verify.fio"
COPY_DIRECTORY = "/tmp"
DEFAULT_AMI = '099720109477/ubuntu/images/hvm-ssd/ubuntu-bionic-18.04-amd64-server-20190212.1'
DEFAULT_NAMESPACE = "change-pd"
WAIT_TIMEOUT = 300

class ProtectionDomainChangeTest:
    def __init__(self, args):
        self.args = args
        self.nuvo_mgmt = NuvoManagement(args)
        self.kubectl_helper = KubectlHelper(args, args.kops_cluster_name)
        #self.nuvo_mgmt.update_snapshot_policy_account()
        self.connect_ssh = ConnectSSH(self.args)
        self.namespace = DEFAULT_NAMESPACE
        self._mapping_tag = yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG

    def dict_representer(self, dumper, data):
        """Yaml representer"""

        return dumper.represent_mapping(self._mapping_tag, data.items())

    def dict_constructor(self, loader, node):
        """Yaml constructor"""

        return collections.OrderedDict(loader.construct_pairs(node))
    
    def install_dependencies(self):
        """Install dependencies"""
        InstallPackages.apt_get_update()
        InstallPackages.install_kops()
        InstallPackages.install_kubectl()
        InstallPackages.install_awscli()
        InstallPackages.configure_aws(self.args)
        InstallPackages.generate_sshkeypair()
        return

    def create_application_cluster(self, app_cluster_name):
        """Create application cluster"""
        KopsCluster.create_kops_app_cluster(self.args)
        try:
            self.nuvo_mgmt.switch_accounts(self.args.tenant_admin)
            csp_domain_id = self.nuvo_mgmt.create_csp_domain()
            nuvo_cluster_name = self.nuvo_mgmt.deploy_clusterd(csp_domain_id)
            logging.info("nuvo_cluster_name: %s", nuvo_cluster_name)
            self.nuvo_mgmt.do_service_plan_allocation(nuvo_cluster_name, self.args.account_name)
            self.nuvo_mgmt.switch_accounts(self.args.account_name)
            protection_domain_id = self.nuvo_mgmt.create_protection_domain()
            self.nuvo_mgmt.set_protection_domain(protection_domain_id, csp_domain_id)
            snapshot_catalog_pd = self.nuvo_mgmt.create_protection_domain()
            self.nuvo_mgmt.set_snapshot_catalog_policy(snapshot_catalog_pd, csp_domain_id)
        except subprocess.CalledProcessError as err:
            if err.output:
                logging.info(err.output)
            raise
        return nuvo_cluster_name

    def write_fio_yamlfile(self, vol_size, volume_name, pvc_name, fio_jobfile, io_size, pod_name):
        """Create yaml with fio's input params"""

        yaml.add_representer(collections.OrderedDict, self.dict_representer)
        yaml.add_constructor(self._mapping_tag, self.dict_constructor)

        app_orig_yaml_path = str(pathlib.Path(self.args.config_dirpath, APP_YAML_TEMPLATE))
        app_yaml_path = str(pathlib.Path(self.args.log_dirpath, APP_YAML_TEMPLATE.split(".")[0] + "-" + \
            volume_name + str(vol_size) +  ".yaml"))
        logging.info("app_yaml_path: %s", app_yaml_path)

        dicts = []
        fio_options = "--directory=%s --aux-path=%s --size=%s --numjobs %s" % (FIO_MOUNTPATH, FIO_MOUNTPATH, io_size/int(self.args.fio_num_jobs), self.args.fio_num_jobs)
        with open(app_orig_yaml_path, 'r') as stream, open(app_yaml_path, 'w') as outfile:
            dicts = list(yaml.load_all(stream))
            for yaml_dict in dicts:
                if yaml_dict['kind'] == 'Pod':
                    yaml_dict['metadata']['namespace'] = DEFAULT_NAMESPACE
                    yaml_dict['metadata']['name'] = pod_name
                    yaml_dict['metadata']['labels']['name'] = pod_name
                    yaml_dict['spec']['containers'][0]['name'] = pod_name
                    yaml_dict['spec']['containers'][0]['image'] = self.args.fio_image
                    yaml_dict['spec']['containers'][0]['env'][0]['value'] = fio_options
                    yaml_dict['spec']['containers'][0]['env'][1]['value'] = fio_jobfile
                    yaml_dict['spec']['containers'][0]['volumeMounts'][0]['name'] = FIO_MOUNTNAME
                    yaml_dict['spec']['containers'][0]['volumeMounts'][0]['mountPath'] = FIO_MOUNTPATH
                    yaml_dict['spec']['volumes'][0]['name'] = FIO_MOUNTNAME
                    yaml_dict['spec']['volumes'][0]['persistentVolumeClaim']['claimName'] = pvc_name
                    yaml_dict['spec']['volumes'][1]['hostPath']['path'] = COPY_DIRECTORY
                elif yaml_dict['kind'] == 'PersistentVolumeClaim':
                    # PVC changes
                    raise ValueError("Yaml template seems old/deprecated")
                else:
                    raise ValueError("Yaml template file: %s is unsupported" % app_orig_yaml_path)
            yaml.dump_all(dicts, outfile, default_flow_style=False)
        return app_yaml_path

    def check_pods(self, pod_names):
        """Check fio is running fine in all pods"""

        # We should have 1 pod for each volume.
        num_pods_done = 0
        for pod_name in pod_names:
            try:
                logging.info("------ Start checking Pod Name: %s ------", pod_name)
                self.kubectl_helper.check_pod_running(pod_name, self.namespace)

                # Check fio showsup in 'ps -ef' command inside the container
                cmd_ps = "ps -ef | grep fio"
                result = self.kubectl_helper.run_kubectl_exec_cmd(cmd_ps, pod_name, pod_name, namespace=self.namespace)
                logging.info(result)
                if not result or "fio" not in result:
                    raise Exception("No output when cmd: %s "
                                    "was run in pod: %s" % (cmd_ps, pod_name))
                logging.info("O/P of 'ps -ef' cmd shows fio is running in pod: %s", pod_name)

                # Show timestamp on fio o/p file
                cmd_ls = "ls -lrt " + FIO_MOUNTPATH
                result = self.kubectl_helper.run_kubectl_exec_cmd(cmd_ls, pod_name, pod_name, namespace=self.namespace)
                logging.info(result)
                if not result or len(result) < 1:
                    raise Exception("No output when cmd: %s "
                                    "was run in pod: %s. Result: %s" % (cmd_ls, pod_name, result))
                logging.info("ls -lrt of 'datadir' ran fine in pod: %s", pod_name)

                # Show container's logs
                result = self.kubectl_helper.run_kubectl_logs_cmd(pod_name, pod_name, namespace=self.namespace)
                logging.info(result)
                if not result or "error" in result:
                    raise Exception("No output OR found 'error' while collecting logs "
                                    "from pod: %s" % pod_name)
                logging.info("kubectl log shows fio stats - fine in pod: %s",
                             pod_name)
                logging.info("------ Done checking Pod Name: %s ------", pod_name)
            except subprocess.CalledProcessError as err:
                time.sleep(5)
                if not self.kubectl_helper.check_pod_completed(pod_name, namespace=self.namespace):
                    raise
                logging.info(err.output)
                logging.info("Pod name: %s has completed its run. Container's "
                             "log:", pod_name)
                result = self.kubectl_helper.run_kubectl_logs_cmd(pod_name, pod_name, namespace=self.namespace)
                logging.info(result)
                num_pods_done = num_pods_done + 1
                logging.info("num_pods_done: %d", num_pods_done)
                logging.info("------ Done checking Pod Name: %s ------", pod_name)
                if num_pods_done == len(self.args.list_volume_sizes):
                    return True
                else:
                    # check remaining pods
                    pass

    def copy_jobfile_to_nodes(self, jobfile_name):
        """Copy fio job files to app cluster nodes"""
        node_list = self.kubectl_helper.get_nodes_cluster()
        for node in node_list:
            hostname = self.kubectl_helper.get_node_external_dns("kubernetes.io/hostname", node)
            logging.info("Copying to %s", hostname)
            self.connect_ssh.scp_copy_to_node(hostname, str(pathlib.Path(self.args.config_dirpath, jobfile_name)), COPY_DIRECTORY)
        return 

    def get_current_pd_id(self, account_name, csp_domain_id):
        """Returns the current protection domain id for the specified csp domain id"""
        account_list = self.nuvo_mgmt.get_all_accounts()
        for account in account_list:
            if account['name'] == account_name:
                break
        pd = account['protectionDomains'][csp_domain_id]
        return pd
    
    def wait_for_volume_snapshots(self, volume_name, volume_size):
        """Wait for snapshots to be created and uploaded for the specified volume"""
        timeout = int(volume_size) * WAIT_TIMEOUT
        time_now = datetime.datetime.utcnow()
        volume_state_good = False
        logging.info("Check if volume %s is in PROVISIONED state", volume_name)
        while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(seconds=timeout)):
            volume_status = self.nuvo_mgmt.get_volume_state(volume_name)
            if volume_status == "PROVISIONED":
                logging.info("Volume state is now PROVISIONED. Snapshots should be created and uploaded.")
                volume_state_good = True
                break
            else:
                time.sleep(60)
                logging.info("Volume state is now %s. Re-check after 60s.", volume_status)
        if not volume_state_good:
            raise RuntimeError("Timeout reached: Volume state is %s after %ss and not in UNBOUND state as expected", volume_status, timeout)

    def run_pd_change_test(self):
        """Run protection domain change test"""

        nuvo_cluster_name = self.args.nuvo_cluster
        csp_domain_id = self.nuvo_mgmt.get_csp_domain_id_from_cluster(nuvo_cluster_name)
        self.nuvo_mgmt.switch_accounts(self.args.tenant_admin)
        spa_id = self.nuvo_mgmt.do_service_plan_allocation(nuvo_cluster_name, self.args.account_name)
        self.nuvo_mgmt.switch_accounts(self.args.account_name)

        self.kubectl_helper.create_namespace(self.namespace)
        self.nuvo_mgmt.launch_secret_yaml(nuvo_cluster_name, namespace=self.namespace)

        self.copy_jobfile_to_nodes(WRITE_JOB)
        self.copy_jobfile_to_nodes(READ_JOB)
        volumes = []
        pod_names = []
        pvc_names = []
        for vol_size in self.args.list_volume_sizes:    
            logging.info("Creating a dynamically provisioned volume")
            pvc_name_prefix="pd-change-pvc-"
            pvc_name = pvc_name_prefix + str(uuid.uuid4())[:5]
            volume_name = self.nuvo_mgmt.launch_volume_pvc_dynamic(pvc_name, spa_id, vol_size=vol_size, namespace=self.namespace)
            logging.info("volume_name: %s, pvc_name: %s", volume_name, pvc_name)
            volumes.append(volume_name)

            pod_name = self.args.fio_name + "-write-" + vol_size
            pod_names.append(pod_name) 
            pvc_names.append(pvc_name)
            io_size = 0.8 * (int(vol_size) << 30)
            app_yaml_path = self.write_fio_yamlfile(vol_size, volume_name, pvc_name, WRITE_JOB, io_size, pod_name)
            self.kubectl_helper.deploy_appcontainer(app_yaml_path, volume_name, \
                pod_name=pod_name, pvc_name=pvc_name, dynamic=True, vol_id=None, namespace=self.namespace)
            curr_node = self.kubectl_helper.get_scheduled_node(pod_name, namespace=self.namespace)
            logging.info("Pod %s is scheduled on node %s", pod_name, curr_node)

        all_pods_done = False
        time_now = datetime.datetime.utcnow()
        while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(
                seconds=int(self.args.fio_timeout) + TEST_RUNTIME_DELTA)):
            logging.info("========================")
            if self.check_pods(pod_names):
                all_pods_done = True
                logging.info("All pods have completed successfully.")
                break
            else:
                logging.info("Sleeping for %s before checking the pods again...",
                             self.args.fio_sleep_time)
                time.sleep(self.args.fio_sleep_time)

        for i in range(len(pod_names)):
            self.kubectl_helper.delete_pod(pod_names[i], namespace=self.namespace)
            self.kubectl_helper.delete_pvc(pvc_names[i], namespace=self.namespace)
            self.nuvo_mgmt.bind_and_publish_volume(volumes[i], nuvo_cluster_name)

        current_pd_id = self.get_current_pd_id(self.args.account_name, csp_domain_id)
        for volume_name in volumes:
            latest_snapshot = self.nuvo_mgmt.get_snapshots(volume_name=volume_name, sort_field='snapTime', sort_order='desc')[0]
            if latest_snapshot['protectionDomainId'] != current_pd_id:
                raise ValueError("Protection domain for latest snapshot not as expected")

        # Change protection domain
        new_protection_domain_id = self.nuvo_mgmt.create_protection_domain()
        self.nuvo_mgmt.set_protection_domain(new_protection_domain_id, csp_domain_id)
        
        read_pod_names = []
        for i in range(len(self.args.list_volume_sizes)):
            vol_size = self.args.list_volume_sizes[i]
            pod_name = self.args.fio_name + "-read-" + str(vol_size)
            read_pod_names.append(pod_name)
            pvc = self.nuvo_mgmt.launch_volume_pvc(volumes[i], namespace=self.namespace)
            vol_id = self.nuvo_mgmt.get_volume_id(volumes[i])
            logging.info("volume_name: %s, pvc_name: %s", volumes[i], pvc)

            # create the app yaml file based off the template
            io_size = 0.8 * (int(vol_size) << 30)
            app_yaml_path = self.write_fio_yamlfile(vol_size, volumes[i], pvc, READ_JOB, io_size, pod_name)
            # Start read verify job
            self.kubectl_helper.deploy_appcontainer(app_yaml_path, volumes[i],
                    pod_name=pod_name, pvc_name=pvc, dynamic=False, vol_id=vol_id, namespace=self.namespace)

        # check for pods completion and exit

        all_pods_done = False
        time_now = datetime.datetime.utcnow()
        while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(
                seconds=int(self.args.fio_timeout) + TEST_RUNTIME_DELTA)):
            logging.info("========================")
            if self.check_pods(read_pod_names):
                all_pods_done = True
                logging.info("All fio read pods have completed successfully.")
                break
            else:
                logging.info("Sleeping for %s before checking the pods again...",
                             self.args.fio_sleep_time)
                time.sleep(self.args.fio_sleep_time)

        for pod in read_pod_names:
            self.kubectl_helper.delete_pod(pod, namespace=self.namespace)

        # Wait for snapshots to finish and volumes to go to PROVISIONED state and verify protection domain id in the snapshot
        for i in range(len(volumes)):
            self.wait_for_volume_snapshots(volumes[i], self.args.list_volume_sizes[i])
            latest_snapshot = self.nuvo_mgmt.get_snapshots(volume_name=volume_name, sort_field='snapTime', sort_order='desc')[0]
            if latest_snapshot['protectionDomainId'] != new_protection_domain_id:
                logging.error("Protection domain found: %s Expected: %s", latest_snapshot['protectionDomainId'], new_protection_domain_id)
                raise ValueError("Protection domain for latest snapshot not as expected.")
                    
        if all_pods_done:
            logging.info("SUCCESS: All pods with fio ran fine.")
            # cleanup
            if self.args.do_cleanup:
                logging.info("Test succeeded. Deleting kops cluster now")
                KopsCluster.kops_delete_cluster(kops_cluster_name=self.args.app_cluster,
                                                kops_state_store=self.args.kops_state_store)
                domain_id = self.nuvo_mgmt.get_csp_domain_id_from_cluster(self.args.nuvo_cluster)
                self.nuvo_mgmt.wait_for_cluster_state(self.args.nuvo_cluster, "TIMED_OUT", domain_id=domain_id)
                self.kubectl_helper.cleanup_cluster(self.args.nuvo_cluster, \
                    self.nuvo_mgmt.get_csp_domain(domain_id)[0]['name'] , self.args.tenant_admin)
            else:
                logging.info("Test succeeded. Skipping kops delete cluster since " 
                                "do_cleanup is False")
        else:
            raise RuntimeError("Some/all pods did not reach 'Completed' state within time limit")
        
def main():
    """main"""

    parser = argparse.ArgumentParser(description="Deploys a kops cluster with "
                                     "Nuvo data plane and runs fio against all volumes")
    parser.add_argument(
        '--app_cluster', help='Name of app cluster from which volume will be unbound and published')
    parser.add_argument(
        '--nuvo_cluster', help='Name of cluster object, if created in the kontroller for app_cluster1')
    parser.add_argument(
        '--nodes', help='Number of nodes in the cluster [default=3]', type=int, default=3,
        choices=range(1, 101))
    parser.add_argument(
        '--kops_state_store', help='state store for both clusters')
    parser.add_argument(
        '--aws_access_key', help='aws AccessKey')
    parser.add_argument(
        '--aws_secret_access_key', help='aws SecretAccessKey')
    parser.add_argument(
        '--region', help='aws region', default='us-west-2')
    parser.add_argument(
        '--k8s_master_zone', help='aws zone for master node',
        default='us-west-2c')
    parser.add_argument(
        '--k8s_nodes_zone', help='aws zone for other nodes ',
        default='us-west-2c')
    parser.add_argument(
        '--master_size', help='ec2 instance type for master node ', default='m5d.large')
    parser.add_argument(
        '--node_size', help='ec2 instance type for other nodes ', default='m5d.large')
    parser.add_argument(
        '--nuvo_kontroller_hostname', help='Hostname of https svc of Nuvo Kontroller',
        required=True)
    parser.add_argument(
        '--config_dirpath', help='directory path to user-app yaml files.'
        '[default=${HOME}/testing/config/')
    parser.add_argument(
        '--account_name', help='Nuvoloso account name', default='Normal Account')
    parser.add_argument(
        '--tenant_admin', help='Tenant admin account name for doing service plan allocations', default='Demo Tenant')
    parser.add_argument(
        '--volume_sizes', help='Comma separated list of volume sizes', default="10")
    parser.add_argument(
        '--fio_image', help='Path to Docker image for fio container '
        'default: 407798037446.dkr.ecr.us-west-2.amazonaws.com/nuvolosotest/fiotest:v3', 
        default='407798037446.dkr.ecr.us-west-2.amazonaws.com/nuvolosotest/fiotest:v3')
    parser.add_argument(
        '--fio_timeout', help='Time in seconds, in addtion to a delta of {0}s to wait for the fio job(s) to complete. \
        [DEFAULT=300] based on default volume sizes'.format(TEST_RUNTIME_DELTA), default=300)
    parser.add_argument(
        '--fio_sleep_time', help='Sleep in seconds [default=120] '
        'before checking the pods again', type=int, default=120)
    parser.add_argument(
        '--fio_name', help='Name for the fio pods', default="fiopd")
    parser.add_argument(
        '--log_dirpath', help='log dir to hold test and nuvo logs', default=None)
    parser.add_argument(
        '--do_cleanup', help='Delete both app clusters in the end',
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
        '--serviceplan_name', help='Name of service plan ', default='General')
    parser.add_argument(
        '--fio_num_jobs', help='Number of fio jobs. [Default: 10]', default=10)

    args = parser.parse_args()
    try:
        assert(args.nuvo_cluster and args.app_cluster)
    except:
        assert(args.kops_state_store and args.aws_access_key
           and args.aws_secret_access_key), "Some/all input parameters are not filled. Aborting"

    home_dir = str(pathlib.Path.home())
    if not args.config_dirpath:
        args.config_dirpath = str(pathlib.Path(__file__).parent.absolute()/'../config/')

    args.log_dirpath = args.log_dirpath if args.log_dirpath else home_dir + "/logs-" + \
            str(uuid.uuid4())[:5] + "/"
    pathlib.Path(args.log_dirpath).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(format='%(asctime)s %(message)s', filename=args.log_dirpath + "/" +
                        os.path.basename(__file__) + ".log", level=logging.INFO)
    args.list_volume_sizes = args.volume_sizes.strip().split(",")
    assert(args.list_volume_sizes), "'volume_sizes' param was empty"
    logging.info("len args.list_volume_sizes: %d", len(args.list_volume_sizes))
    logging.info("args.list_volume_sizes: %s", args.list_volume_sizes)
    logging.info("args.fio_timeout: %s", args.fio_timeout)
    args.kops_cluster_name = args.app_cluster
    logging.info(args)

    print("Script to test protection domain change")
    test = ProtectionDomainChangeTest(args)
    if not args.nuvo_cluster:
        logging.info("Creating Application cluster")
        test.install_dependencies()
        args.nuvo_cluster = test.create_application_cluster(args.app_cluster)
    test.run_pd_change_test()


if __name__ == '__main__':
    main()
