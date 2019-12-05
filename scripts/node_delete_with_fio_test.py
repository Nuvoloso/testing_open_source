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

Test to verify node recovery with an application running. The expectation is that on a node failure, 
the application gets respawned with the appropriate storage and continue execution.

Prerequisites :
1. Requires a kontroller hostname. A K8s cluster with kontroller software which supports rei should be deployed.
To deploy one, use deploy_nuvo_kontroller.py with the correct deployment yaml file.
2. PYTHONPATH needs to be set in the environment (export PYTHONPATH=/home/ubuntu/testing/lib)
3. Optionally, application cluster(s) can be deployed as well using deploy_app_cluster.py

Example:

PYTHONPATH=~/testing/lib/ python3 ~/testing/scripts/node_delete_with_fio_test.py --nuvo_kontroller_hostname ac79fe4bbd5a211e98ab40ac475462be-1424111265.us-west-2.elb.amazonaws.com  
--app_cluster test.k8s.local --mgmt_cluster mgmt.k8s.local --kops_state_store s3://nri-test-bucket --aws_access_key XXXXX --aws_secret_access_key XXXXXX
--fio_sleep_time 10 --volume_size 2 --nuvo_cluster nuvoauto-7f2bd -c 6

To use an existing app cluster, use the --nuvo_cluster to pass the cluster name created in the kontroller

PYTHONPATH=~/testing/lib/ python3 ~/testing/scripts/node_delete_with_fio_test.py --nuvo_kontroller_hostname ac79fe4bbd5a211e98ab40ac475462be-1424111265.us-west-2.elb.amazonaws.com  
--app_cluster test.k8s.local --mgmt_cluster mgmt.k8s.local --kops_state_store s3://nri-test-bucket --aws_access_key XXXXX --aws_secret_access_key XXXXXX
--fio_sleep_time 10 --volume_size 2 -c 6

To view the list of testcases, use --view_testcases option.


Check help for other optional parameters.

Test Steps:
1. Deploy a job running fio writes with a dynamically provisioned/static volume.
2. Enable the appropriate rei.
3. Wait for the appropriate conditon based on the testcase, and terminate the node where fio is running.
4. Check whether another pod is respawned on a different node and that it's running.
5. Wait till it finishes successfully.
6. Start fio reads with verify and ensure that there are no data integrity issues. 
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
from nuvoloso.api.obj_helper import NodeObject, VolumeSeriesRequestObject
from nuvoloso.dependencies.kubectl_helper import KON_NAMESPACE as NS_MANAGEMENT
from nuvoloso.dependencies.aws import AWS

FIO_MOUNTPATH = "/datadir"
FIO_MOUNTNAME = "datavol"
TEST_RUNTIME_DELTA = 10800
APP_YAML_TEMPLATE = "fio_job.yaml"
WRITE_JOB = "write_verify.fio"
READ_JOB = "read_verify.fio"
COPY_DIRECTORY = "/tmp"
DEFAULT_AMI = '099720109477/ubuntu/images/hvm-ssd/ubuntu-bionic-18.04-amd64-server-20190212.1'
DEFAULT_NAMESPACE = "node-del"
WAIT_TIMEOUT = 300

DEF_CENTRALD_CONTAINER = "centrald"
DEF_CENTRALD_POD = "services-0"
DEF_CLUSTERD_CONTAINER = "clusterd"
DEF_CLUSTERD_POD = "clusterd-0"

REI_SREQ_BLOCK_IN_ATTACH = 'sreq/block-in-attach'
REI_SREQ_BLOCK_NUVO_CLOSE_DEVICE = 'sreq/block-nuvo-close-device'
REI_SREQ_BLOCK_NUVO_USE_DEVICE = 'sreq/block-nuvo-use-device'
REI_SREQ_FORMAT_TMP_ERROR = 'sreq/format-tmp-error'
REI_VREQ_PLACEMENT_BLOCK_ON_VS_UPDATE = 'vreq/placement-block-on-vs-update'
REI_VREQ_SIZE_BLOCK_ON_START = 'vreq/size-block-on-start'
REI_VREQ_TEARDOWN_FATAL_ERROR = 'vreq/teardown-fatal-error'
REI_VREQ_VSC_BLOCK_ON_START = 'vreq/vsc-block-on-start'


AGENTD_SR_CLOSE = "block_in_agentd_sr_close_device"
AGENTD_VSR_START = "block_in_agentd_vsr_start"
CLUSTERD_VSR_IN_TRANSITION = "clusterd_block_vsr_in_transition"
AGENTD_SR_USE_DEVICE = "block_in_agentd_sr_use_device"
AGENTD_FORMAT = "block_in_agentd_format"
CENTRALD_ATTACH = "block_in_centrald_attach"
CLUSTERD_SIZING = "block_in_clusterd_size"
NODE_DEL_FAILURE = "node_delete_failure_in_centrald"

class NodeDeleteWithFio:
    def __init__(self, args):
        self.args = args
        self.nuvo_mgmt = NuvoManagement(args)
        self.kubectl_helper = KubectlHelper(args, args.kops_cluster_name)
        self.mgmt_kubectl_helper = KubectlHelper(args, args.mgmt_cluster)
        self.connect_ssh = ConnectSSH(self.args)
        self.namespace = DEFAULT_NAMESPACE
        self._mapping_tag = yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG
        self.node_obj = NodeObject()
        self._aws = None
        self.testcases = dict(
            block_in_agentd_sr_close_device=REI_SREQ_BLOCK_NUVO_CLOSE_DEVICE,
            block_in_agentd_vsr_start=REI_VREQ_VSC_BLOCK_ON_START,
            block_in_agentd_sr_use_device=REI_SREQ_BLOCK_NUVO_USE_DEVICE,
            block_in_agentd_format=REI_SREQ_FORMAT_TMP_ERROR,
            clusterd_block_vsr_in_transition=REI_VREQ_PLACEMENT_BLOCK_ON_VS_UPDATE,
            block_in_centrald_attach=REI_SREQ_BLOCK_IN_ATTACH,
            block_in_clusterd_size=REI_VREQ_SIZE_BLOCK_ON_START,
            node_delete_failure_in_centrald=REI_VREQ_TEARDOWN_FATAL_ERROR
        )
        self.testcase_list = NodeDeleteWithFio.setup_cases()

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

    def create_application_cluster(self):
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
                if yaml_dict['kind'] == 'Job':
                    yaml_dict['metadata']['namespace'] = DEFAULT_NAMESPACE
                    yaml_dict['metadata']['name'] = pod_name
                    yaml_dict['metadata']['labels']['name'] = pod_name
                    template = yaml_dict['spec']['template']
                    template['metadata']['labels']['name'] = pod_name
                    template['spec']['containers'][0]['name'] = pod_name
                    template['spec']['containers'][0]['image'] = self.args.fio_image
                    template['spec']['containers'][0]['env'][0]['value'] = fio_options
                    template['spec']['containers'][0]['env'][1]['value'] = fio_jobfile
                    template['spec']['containers'][0]['volumeMounts'][0]['name'] = FIO_MOUNTNAME
                    template['spec']['containers'][0]['volumeMounts'][0]['mountPath'] = FIO_MOUNTPATH
                    template['spec']['volumes'][0]['name'] = FIO_MOUNTNAME
                    template['spec']['volumes'][0]['persistentVolumeClaim']['claimName'] = pvc_name
                    template['spec']['volumes'][1]['hostPath']['path'] = COPY_DIRECTORY
                else:
                    raise ValueError("Yaml template file: %s is unsupported" % app_orig_yaml_path)
            yaml.dump_all(dicts, outfile, default_flow_style=False)
        return app_yaml_path

    def wait_for_pods(self, job_name):
        """Check fio is running fine in all pods"""

        # We should have 1 pod for each volume.
        all_pods_done = False
        time_now = datetime.datetime.utcnow()
        while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(seconds=int(self.args.fio_timeout) + TEST_RUNTIME_DELTA)):
            logging.info("========================")
            pod_name = self.kubectl_helper.get_pods(label=job_name, namespace=self.namespace).splitlines()[-1].split()[0]
            try:
                logging.info("------ Start checking Pod Name: %s ------", pod_name)
                self.kubectl_helper.check_pod_running(pod_name, self.namespace)

                # Check fio showsup in 'ps -ef' command inside the container
                cmd_ps = "ps -ef | grep fio"
                result = self.kubectl_helper.run_kubectl_exec_cmd(cmd_ps, pod_name, job_name, namespace=self.namespace)
                logging.info(result)
                if not result or "fio" not in result:
                    raise Exception("No output when cmd: %s "
                                    "was run in pod: %s" % (cmd_ps, pod_name))
                logging.info("O/P of 'ps -ef' cmd shows fio is running in pod: %s", pod_name)

                # Show timestamp on fio o/p file
                cmd_ls = "ls -lrt " + FIO_MOUNTPATH
                result = self.kubectl_helper.run_kubectl_exec_cmd(cmd_ls, pod_name, job_name, namespace=self.namespace)
                logging.info(result)
                if not result or len(result) < 1:
                    raise Exception("No output when cmd: %s "
                                    "was run in pod: %s. Result: %s" % (cmd_ls, pod_name, result))
                logging.info("ls -lrt of 'datadir' ran fine in pod: %s", pod_name)

                # Show container's logs
                result = self.kubectl_helper.run_kubectl_logs_cmd(pod_name, job_name, namespace=self.namespace)
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
                else:
                    all_pods_done = True
                    logging.info(err.output)
                    logging.info("Pod name: %s has completed its run. Container's "
                             "log:", pod_name)
                    result = self.kubectl_helper.run_kubectl_logs_cmd(pod_name, job_name, namespace=self.namespace)
                    logging.info(result)
                    logging.info("------ Done checking Pod Name: %s ------", pod_name)
                    break
            logging.info("Sleeping for %s before checking the pods again...",
                         self.args.fio_sleep_time)
            time.sleep(self.args.fio_sleep_time)

        if not all_pods_done:
            raise RuntimeError("Pod did not reach 'Completed' state within time limit of " + str(TEST_RUNTIME_DELTA))


    def aws(self):
        """Returns the cached AWS helper"""
        if self._aws:
            return self._aws
        setattr(self.args, 'region', self.args.region)
        self._aws = AWS(self.args)
        return self._aws

    @classmethod
    def indefinite_sequence(cls, values: list):
        """Return the next of the specified values, repeating the last indefinitely"""
        while len(values) > 1:
            yield values[0]
            values.pop(0)
        while True:
            yield values[0]

    def copy_jobfile_to_nodes(self, jobfile_name):
        """Copy fio job files to app cluster nodes"""
        node_obj = NodeObject()
        node_list = self.kubectl_helper.get_nodes_cluster()
        for node in node_list:
            node_obj = node_obj.query(self.nuvo_mgmt, dict(name=node))[0]
            if node_obj._data['service']['state'] == 'READY':
                hostname = self.kubectl_helper.get_node_external_dns("kubernetes.io/hostname", node)
                logging.info("Copying to %s", hostname)
                self.connect_ssh.scp_copy_to_node(hostname, str(pathlib.Path(self.args.config_dirpath, jobfile_name)), COPY_DIRECTORY)
        return 

    def create_k8s_jobs(self, volume_name, pvc_name, job_name, job_file):
        """ Create Jobs """

        io_size = 0.8 * (int(self.args.volume_size) << 30)
        app_yaml_path = self.write_fio_yamlfile(self.args.volume_size, volume_name, pvc_name, job_file, io_size, job_name)
        self.kubectl_helper.deploy(app_yaml_path)
        pod_name = self.kubectl_helper.get_pods(label=job_name, namespace=self.namespace).splitlines()[-1].split()[0]
        curr_node = self.kubectl_helper.get_scheduled_node(pod_name, namespace=self.namespace)
        logging.info("Pod %s is scheduled on node %s", pod_name, curr_node)
        return curr_node

    def terminate_aws_instance(self, instance_id, vol_id):
        """ Terminate specified AWS instance """

        vsr_obj = VolumeSeriesRequestObject()
        vsr_obj = vsr_obj.query(self.nuvo_mgmt, dict(volumeSeriesid=vol_id))[-1]
        if self.testcase_list[self.args.case_num-1] == CLUSTERD_VSR_IN_TRANSITION:
            vsr_obj.wait(self.nuvo_mgmt, non_terminal_states=['PLACEMENT'])
        elif self.testcase_list[self.args.case_num-1] == AGENTD_VSR_START:
            vsr_obj.wait(self.nuvo_mgmt, non_terminal_states=['PAUSING_IO'])
        elif self.testcase_list[self.args.case_num-1] in [AGENTD_SR_USE_DEVICE, AGENTD_FORMAT, CENTRALD_ATTACH]:
            vsr_obj.wait(self.nuvo_mgmt, non_terminal_states=['STORAGE_WAIT'])
        elif self.testcase_list[self.args.case_num-1] == CLUSTERD_SIZING:
            vsr_obj.wait(self.nuvo_mgmt, non_terminal_states=['SIZING'])
        else:
            time.sleep(120)

        if self.node_obj:
            # the node may have been deleted already
            instance_id = self.node_obj.aws_instance()
            print("Checking AWS instance", instance_id)
            for i in self.aws().get_instances_by_id([instance_id]):
                print('Instance', instance_id, 'state:', i.state)
                if i.state['Name'] == 'running':
                    print("Deleting instance", instance_id)
                    self.aws().terminate_instances([instance_id])
            print("="*78)
            retry_after = self.indefinite_sequence([10, 10, 5])
            while True:
                try:
                    if self.node_obj.service_state() in ['STOPPED', 'UNKNOWN']:
                        break
                    self.node_obj.load(self.nuvo_mgmt) # could throw if the node is deleted
                except RuntimeError:
                    print("Node object error")
                    break
                time.sleep(next(retry_after))
        return 

    def check_pod_state(self, job_name, scheduled_node=None):
        """ Verify if pod is running and re-scheduled on a new node """

        time_now = datetime.datetime.utcnow()
        while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(
                seconds=int(self.args.fio_timeout) + TEST_RUNTIME_DELTA)):
            logging.info("========================")
            pod_name = self.kubectl_helper.get_pods(label=job_name, namespace=self.namespace).splitlines()[-1].split()[0]
            if self.kubectl_helper.check_pod_running(pod_name, namespace=self.namespace):
                logging.info("Pod is running.")
                curr_node = self.kubectl_helper.get_scheduled_node(pod_name, namespace=self.namespace)
                if scheduled_node:
                    if curr_node == scheduled_node:
                        raise RuntimeError("Pod is running on node which is supposed to be terminated. Exiting...")
                break   
            else:
                logging.info("Sleeping for %s before checking the pods again...",
                             self.args.fio_sleep_time)
                time.sleep(self.args.fio_sleep_time)

        return 

    @classmethod
    def setup_cases(cls):
        """List of setup cases in order"""
        return [
            AGENTD_SR_CLOSE,
            AGENTD_VSR_START,
            CLUSTERD_VSR_IN_TRANSITION,
            AGENTD_SR_USE_DEVICE,
            AGENTD_FORMAT,
            CENTRALD_ATTACH,
            CLUSTERD_SIZING,
            NODE_DEL_FAILURE
        ]

    def setup_rei(self, vs_id=None):
        rei = self.testcase_list[self.args.case_num-1]
        logging.info("Setting up REI %s", self.testcases[rei])
        if "clusterd" in rei:
            if rei == CLUSTERD_SIZING:
                self.kubectl_helper.rei_set(self.testcases[rei], DEF_CLUSTERD_POD, container_name=DEF_CLUSTERD_CONTAINER, bool_value=True, do_not_delete=True)
            if rei == CLUSTERD_VSR_IN_TRANSITION:
                if not vs_id:
                    raise ValueError("Volume series id cannot be null")
                self.kubectl_helper.rei_set(self.testcases[rei], DEF_CLUSTERD_POD, container_name=DEF_CLUSTERD_CONTAINER, string_value=vs_id, do_not_delete=True)
        if "agentd" in rei:
            self.kubectl_helper.rei_set(self.testcases[rei], self.node_obj.service_pod(), bool_value=True, do_not_delete=True)
        if "centrald" in rei:
            self.mgmt_kubectl_helper.rei_set(self.testcases[rei], DEF_CENTRALD_POD, container_name=DEF_CENTRALD_CONTAINER, namespace=NS_MANAGEMENT, bool_value=True, do_not_delete=True)
        return
    
    def remove_rei(self):
        rei = self.testcase_list[self.args.case_num-1]
        logging.info("Removing REI %s", self.testcases[rei])
        try:
            if "clusterd" in rei:
                self.kubectl_helper.rei_clear(self.testcases[rei], DEF_CLUSTERD_POD, container_name=DEF_CLUSTERD_CONTAINER)
            if "agentd" in rei:
                self.kubectl_helper.rei_clear(self.testcases[rei], self.node_obj.service_pod())
            if "centrald" in rei:
                self.mgmt_kubectl_helper.rei_clear(self.testcases[rei], DEF_CENTRALD_POD, container_name=DEF_CENTRALD_CONTAINER, namespace=NS_MANAGEMENT)
        except:
            pass

        return 

    def run_node_delete_test(self):
        """Run protection domain change test"""

        nuvo_cluster_name = self.args.nuvo_cluster
        csp_domain_id = self.nuvo_mgmt.get_csp_domain_id_from_cluster(nuvo_cluster_name)
        self.nuvo_mgmt.switch_accounts(self.args.tenant_admin)
        spa_id = self.nuvo_mgmt.do_service_plan_allocation(nuvo_cluster_name, self.args.account_name)
        self.nuvo_mgmt.switch_accounts(self.args.account_name)

        self.kubectl_helper.create_namespace(self.namespace)
        self.nuvo_mgmt.launch_secret_yaml(nuvo_cluster_name, namespace=self.namespace)
        
        if self.args.case_num == 3 or self.args.static:
            logging.info("Creating a pre-provisioned volume")
            volume_name = self.nuvo_mgmt.create_volume(nuvo_cluster_name, vol_size=self.args.volume_size)
            # launch volume in k8s
            pvc_name = self.nuvo_mgmt.launch_volume_pvc(volume_name, namespace=self.namespace)
            logging.info("volume_name: %s, pvc_name: %s", volume_name, pvc_name)
            volume_series_id = self.nuvo_mgmt.get_volume_id(volume_name)
        else:
            logging.info("Creating a dynamically provisioned volume")
            pvc_name_prefix = "nuvoauto-pvc-"
            pvc_name = pvc_name_prefix + str(uuid.uuid4())[:5]
            # launch volume in k8s
            volume_name = self.nuvo_mgmt.launch_volume_pvc_dynamic(pvc_name, spa_id, vol_size=self.args.volume_size, namespace=self.namespace)
            volume_series_id = None

        # Create k8s jobs for writes
        job_name = "fio-write-del"
        self.copy_jobfile_to_nodes(WRITE_JOB)
        scheduled_node = self.create_k8s_jobs(volume_name, pvc_name, job_name , WRITE_JOB)

        # Delete node when it is in the right state.
        self.node_obj = self.node_obj.query(self.nuvo_mgmt, dict(name=scheduled_node))[0]
        
        # Set REI
        if self.testcase_list[self.args.case_num-1] in [
            CLUSTERD_VSR_IN_TRANSITION,
            AGENTD_SR_USE_DEVICE,
            AGENTD_FORMAT,
            CENTRALD_ATTACH,
            CLUSTERD_SIZING]:
            self.setup_rei(volume_series_id)
            self.terminate_aws_instance(self.node_obj.aws_instance(), volume_series_id)
            self.remove_rei()
            # Verify pod is re-created on another node and is running.
            self.check_pod_state(job_name, scheduled_node)
        else:
            self.check_pod_state(job_name)

        if self.testcase_list[self.args.case_num-1] == AGENTD_VSR_START:
            self.setup_rei(volume_series_id)

        self.wait_for_pods(job_name)
        
        if self.testcase_list[self.args.case_num-1] == NODE_DEL_FAILURE:
            self.setup_rei(volume_series_id)
            self.terminate_aws_instance(self.node_obj.aws_instance(), volume_series_id)

        self.kubectl_helper.delete_resource('job', job_name, namespace=self.namespace)
        if self.testcase_list[self.args.case_num-1] == AGENTD_VSR_START:
            self.terminate_aws_instance(self.node_obj.aws_instance(), volume_series_id)

        if self.testcase_list[self.args.case_num-1] == AGENTD_SR_CLOSE:
            self.setup_rei(volume_series_id)
            self.kubectl_helper.delete_pvc(pvc_name, namespace=self.namespace)
            self.terminate_aws_instance(self.node_obj.aws_instance(), volume_series_id)
            self.nuvo_mgmt.bind_and_publish_volume(volume_name, nuvo_cluster_name)
            pvc_name = self.nuvo_mgmt.launch_volume_pvc(volume_name, namespace=self.namespace)
        
        # start read pods
        job_name = "fio-read-del"
        self.copy_jobfile_to_nodes(READ_JOB) 
        self.create_k8s_jobs(volume_name, pvc_name, job_name , READ_JOB)
        self.check_pod_state(job_name)
        # check for pods completion and exit
        self.wait_for_pods(job_name)

        self.kubectl_helper.delete_resource('job', job_name, namespace=self.namespace)
        # cleanup
        try:
            self.remove_rei()
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
        except:
            pass
        
def main():
    """main"""

    parser = argparse.ArgumentParser(description="Deploys a kops cluster with Nuvo data plane and runs fio against all volumes")
    parser.add_argument('-A', '--app_cluster', help='Name of app cluster from which volume will be unbound and published', required=True)
    parser.add_argument('-M', '--mgmt_cluster', help='Name of the cluster where kontroller is deployed', required=True)
    parser.add_argument('-N', '--nuvo_cluster', help='Name of cluster object, if created in the kontroller for app_cluster')
    parser.add_argument('--nodes', help='Number of nodes in the cluster [default=3]', type=int, default=3, choices=range(1, 101))
    parser.add_argument('--kops_state_store', help='state store for both clusters')
    parser.add_argument('--aws_access_key', help='aws AccessKey')
    parser.add_argument('--aws_secret_access_key', help='aws SecretAccessKey')
    parser.add_argument('--region', help='aws region', default='us-west-2')
    parser.add_argument('--k8s_master_zone', help='aws zone for master node', default='us-west-2c')
    parser.add_argument('--k8s_nodes_zone', help='aws zone for other nodes ', default='us-west-2c')
    parser.add_argument('--master_size', help='ec2 instance type for master node ', default='m5d.large')
    parser.add_argument('--node_size', help='ec2 instance type for other nodes ', default='m5d.large')
    parser.add_argument('-H', '--nuvo_kontroller_hostname', help='Hostname of https svc of Nuvo Kontroller', required=True)
    parser.add_argument('--config_dirpath', help='directory path to user-app yaml files. [default=${HOME}/testing/config/')
    parser.add_argument('--account_name', help='Nuvoloso account name', default='Normal Account')
    parser.add_argument('--tenant_admin', help='Tenant admin account name for doing service plan allocations', default='Demo Tenant')
    parser.add_argument('--volume_size', help='Volume size for volumes to be created. Default is 10G', default="10")
    parser.add_argument('--fio_image', help='Path to Docker image for fio container default: 407798037446.dkr.ecr.us-west-2.amazonaws.com/nuvolosotest/fiotest:v3', 
        default='407798037446.dkr.ecr.us-west-2.amazonaws.com/nuvolosotest/fiotest:v3')
    parser.add_argument('--fio_timeout', help='Time in seconds, in addtion to a delta of {0}s to wait for the fio job(s) to complete. \
        [DEFAULT=300] based on default volume sizes'.format(TEST_RUNTIME_DELTA), default=300)
    parser.add_argument('--fio_sleep_time', help='Sleep in seconds [default=10] before checking the pods again', type=int, default=10)
    parser.add_argument('--k8s_job_name', help='Name for the fio pods', default="fio-write-del")
    parser.add_argument('--log_dirpath', help='log dir to hold test and nuvo logs', default=None)
    parser.add_argument('--do_cleanup', help='Delete both app clusters in the end', action='store_true', default=False)
    parser.add_argument('--kubernetes_version', help='version of kubernetes to deploy', default=None)
    parser.add_argument('--image', help='AMI Image for all instances', default=DEFAULT_AMI)
    parser.add_argument('--node_volume_size', help='volume size for slave nodes of k8s cluster', type=int, default=10)
    parser.add_argument('--master_volume_size', help='volume size for master node of k8s cluster', type=int, default=20)
    parser.add_argument('--serviceplan_name', help='Name of service plan ', default='General')
    parser.add_argument('--fio_num_jobs', help='Number of fio jobs. [Default: 10]', default=10)
    parser.add_argument('--aws_profile_name', help='Name of the AWS profile in the credential file', default=None)
    parser.add_argument('--static', action='store_true', default=False, help='Flag to create pre-provisioned volumes instead of using dynamic provisioning')
    parser.add_argument('-c' , '--case_num', help="Testcase to run from the list. Use the --view_testcases option to see the list. Default: 1", choices=range(1,9), type=int, default=1)
    parser.add_argument('--view_testcases', action='store_true', default=False, help="To view list of testcases available. The number can be passed to case_num")
    args = parser.parse_args()
    
    if args.view_testcases:
        cases = NodeDeleteWithFio.setup_cases()
        for case in enumerate(cases):
            print("{}: {}".format(case[0]+1, case[1]))
        return

    try:
        assert args.nuvo_cluster
    except:
        assert args.kops_state_store, "Required argument: kops_state_store"
        assert args.aws_access_key, "Required argument: AWS access key"
        assert args.aws_secret_access_key, "Required argument: AWS secret access key"

    home_dir = str(pathlib.Path.home())
    if not args.config_dirpath:
        args.config_dirpath = str(pathlib.Path(__file__).parent.absolute()/'../config/')

    args.log_dirpath = args.log_dirpath if args.log_dirpath else home_dir + "/logs-" + \
            str(uuid.uuid4())[:5] + "/"
    pathlib.Path(args.log_dirpath).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(format='%(asctime)s %(message)s', filename=args.log_dirpath + "/" +
                        os.path.basename(__file__) + ".log", level=logging.INFO)
    logging.info("args.volume_size: %s", args.volume_size)
    logging.info("args.fio_timeout: %s", args.fio_timeout)
    args.kops_cluster_name = args.app_cluster
    logging.info(args)

    print("Script to test node deletion")
    test = NodeDeleteWithFio(args)
    if not args.nuvo_cluster:
        logging.info("Creating Application cluster")
        test.install_dependencies()
        args.nuvo_cluster = test.create_application_cluster()
    test.run_node_delete_test()


if __name__ == '__main__':
    main()
