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

Collects logs from control plane (e.g. centrald log) and dataplane (e.g. nuvo log)
For nuvo, it also collects nuvo-log file from before a nuvo-crash.
Also collects earliest MAX_NUM_CORES_COLLECTED (=10) Nuvo core files (after compressing them).

Dependency:
When this tool starts, kubectl's current context needs to be that of dataplane
cluster (i.e. nuvoloso-cluster) and not management cluster (i.e. NOT nuvoloso-managment)

Example of a run command:
python3 testing/testingtools/collect_support_bundle.py  \
--kops_state_store s3://kops-neha-nuvoloso --aws_access_key AKIEKA \
--aws_secret_access_key 1vdXXbDU+A  --region us-west-2 \
--nuvo_kontroller_hostname a0358956.us-west-2.elb.amazonaws.com
"""

import argparse
import logging
import json
import os
import pathlib
import time
import subprocess
import uuid
import sys
import boto3

from nuvoloso.dependencies.install_packages import InstallPackages
from nuvoloso.dependencies.kubectl_helper import KubectlHelper
from nuvoloso.nuvoloso.nuvoloso_helper import NuvolosoHelper
from nuvoloso.dependencies.aws import AWS
from nuvoloso.nuvoloso.connect_ssh import ConnectSSH

CONFIGDB = "configdb"
METRICSDB = "metricsdb"
SERVICES = "services"
CLUSTERD = "clusterd-0"
CHECK_OUTPUT_TIMEOUT = 600
WAIT_TIMEOUT = 600
NUVOLOSO_CLUSTER = "nuvoloso-cluster"
NUVOLOSO_MANAGEMENT = "nuvoloso-management"
KUBECTL_LOGS_NUVO = "kubectl logs -n nuvoloso-cluster "
KUBECTL_LOGS_KON = "kubectl logs -n nuvoloso-management "
PATH_NUVO_BINARY = "/opt/nuvoloso/bin/nuvo"
NUVO_CONTAINERS = ["agentd", "nuvo", "csi-driver-registrar"]
KON_CONTAINERS = ["nginx", "centrald", "auth", "webservice"]
CLUSTERD_CONTAINERS = ["clusterd", "csi-provisioner", "csi-attacher"]
KUBECTL_EXEC_NUVO = "kubectl exec -c nuvo -n nuvoloso-cluster "
KUBECTL_GET_PODS = "kubectl get pods "
KUBECTL_GET_PODS_NUVO = "kubectl get pods -n nuvoloso-cluster "
KUBECTL_DESCRIBE_PODS = "kubectl describe pods "
KUBECTL_DESCRIBE_PODS_NUVO = "kubectl describe pods -n nuvoloso-cluster "
KUBECTL_GET_PODS_KON = "kubectl get pods -n nuvoloso-management "
KUBECTL_GET_PODS_KON_LIST = "kubectl get pods -n nuvoloso-management -o name"
KUBECTL_DESCRIBE_PODS_KON = "kubectl describe pods -n nuvoloso-management "
KUBECTL_GET_PV = "kubectl get pv"
KUBECTL_COPY_NUVO = "kubectl cp " + NUVOLOSO_CLUSTER + "/"
MAX_NUM_CORES_COLLECTED = 10
NUVO_CONTAINER = "nuvo"

DEFAULT_REGION = 'us-west-2'

# default login name for AWS host
DEFAULT_LOGIN = 'ubuntu'

class CollectSupportBundle(object):
    """Collect logs & cores from data plane"""

    def __init__(self, args):
        self.args = args
        self.kubectl_helper = KubectlHelper(args)
        self.nuvoloso_helper = NuvolosoHelper(args)
        self.aws = AWS(self.args)
        self.connect_ssh = ConnectSSH(self.args)

    def collect_container_status(self, namespace=NUVOLOSO_CLUSTER):
        """Container status of app and nuvo clusters"""

        if NUVOLOSO_CLUSTER in namespace:
            self.kubectl_helper.get_pods()
            self.kubectl_helper.describe_pods()
            self.kubectl_helper.get_pods(namespace=namespace)
            self.kubectl_helper.get_pv()
            self.kubectl_helper.describe_pods(namespace=namespace)
        elif NUVOLOSO_MANAGEMENT in namespace:
            self.kubectl_helper.get_pods(namespace=namespace)
            self.kubectl_helper.describe_pods(namespace=namespace)
        else:
            raise Exception("Namespace: %s not supported yet" % namespace)

    def collect_controlplane_logs(self):
        """Collect logs from Kontroller"""

        try:
            result = self.kubectl_helper.get_pods(namespace=NUVOLOSO_MANAGEMENT, output='name')
            if not result:
                raise Exception("Get pods from %s failed. Can't get logs. Exiting.." % NUVOLOSO_MANAGEMENT)
            logging.info(result)
            controller_pod_list = [x.split('/')[-1] for x in result.splitlines()]
            logging.info(controller_pod_list)
            for k_pod in controller_pod_list:
                if CONFIGDB in k_pod or METRICSDB in k_pod:
                    logging.info("Collecting logs from %s", k_pod)
                    with open(self.args.log_dirpath + "/" +  k_pod + "-" + \
                              k_pod + ".log", "w") as outfile:
                        outfile.write(self.kubectl_helper.run_kubectl_logs_cmd(k_pod, namespace=NUVOLOSO_MANAGEMENT))
                elif SERVICES in k_pod:
                    # there are multiple containers in this pod
                    for kon_container in KON_CONTAINERS:
                        logging.info("Collecting logs from %s in %s", kon_container, k_pod)
                        with open(self.args.log_dirpath + "/" + kon_container + "-" + \
                            k_pod + ".log", "w") as outfile:
                            outfile.write(self.kubectl_helper.run_kubectl_logs_cmd(k_pod, container_name=kon_container, namespace=NUVOLOSO_MANAGEMENT))
        except subprocess.CalledProcessError as err:
            if err.output:
                logging.error(err.output)
        except:
            logging.error('Failed to collect control plane logs:')
            raise

    def slurp_controlplane_logs(self):
        """Collect logs from Kontroller"""

        try:
            self.args.namespace = 'nuvoloso-management'

            result = self.kubectl_helper.get_pods(namespace=NUVOLOSO_MANAGEMENT, output='name')
            if not result:
                raise Exception("Get pods from %s failed. Can't get logs. Exiting.." % NUVOLOSO_MANAGEMENT)
            logging.info(result)
            controller_pod_list = [x.split('/')[-1] for x in result.splitlines()]
            logging.info(controller_pod_list)

            for k_pod in controller_pod_list:
                logging.info('Looking for containers in %s', k_pod)
                if CONFIGDB in k_pod or METRICSDB in k_pod:
                    self.args.pod = k_pod
                    if 'metricsdb' in k_pod:
                        self.args.container = 'db'
                    elif 'configdb' in k_pod:
                        self.args.container = 'mongo'
                    logging.info("Getting logs for %s", self.args.container)

                    self.args.previous = False
                    private_host, c_id = self.get_host_and_container(self.args)
                    public_host = self.get_aws_host(private_host)
                    self.slurp_logs(public_host, c_id, self.args.pod, self.args.container)

                    # previous
                    self.args.previous = True
                    private_host, c_id = self.get_host_and_container(self.args)
                    if private_host != '':
                        logging.info('Feching previous logs for %s', self.args.container)
                        public_host = self.get_aws_host(private_host)
                        self.slurp_logs(public_host, c_id, self.args.pod, self.args.container)
                elif SERVICES in k_pod:
                    # there are multiple containers in this pod
                    for kon_container in KON_CONTAINERS:
                        logging.info('Getting logs for container: %s', kon_container)
                        self.args.pod = k_pod
                        self.args.container = kon_container
                        logging.info("Getting logs for %s", self.args.container)

                        self.args.previous = False
                        private_host, c_id = self.get_host_and_container(self.args)
                        public_host = self.get_aws_host(private_host)
                        self.slurp_logs(public_host, c_id, self.args.pod, self.args.container)

                        # previous
                        self.args.previous = True
                        private_host, c_id = self.get_host_and_container(self.args)
                        if private_host != '':
                            logging.info('Feching previous logs for %s', self.args.container)
                            public_host = self.get_aws_host(private_host)
                            self.slurp_logs(public_host, c_id, self.args.pod, self.args.container)
        except subprocess.CalledProcessError as err:
            if err.output:
                logging.error(err.output)
        except:
            logging.error("Cannot collect logs for control plane")
            raise

    def get_dataplane_pods(self, skip_clusterd=False):
        """Gets the pods deployed for dataplane"""

        result = self.kubectl_helper.get_pods(namespace=NUVOLOSO_CLUSTER, output='name')
        if not result:
            raise Exception("Failed to get pods for namespace %s. Can't get logs. Exiting.." % NUVOLOSO_CLUSTER)
        logging.info(result)
        nuvo_pod_list = [x.split('/')[-1] for x in result.splitlines()]
        if skip_clusterd:
            nuvo_pod_list.remove(CLUSTERD)
        logging.info(nuvo_pod_list)
        return nuvo_pod_list

    def collect_dataplane_logs(self):
        """Collect clusterd, CSI, nuvo, agentd logs from all the pods in dataplane"""

        result = self.kubectl_helper.get_pods(namespace=NUVOLOSO_CLUSTER)
        if not result:
            raise Exception("Failed to get list of pods from namespace %s. Can't get logs. Exiting.." % NUVOLOSO_CLUSTER)
        logging.info(result)
        nuvo_pod_list = [x.split('/')[-1] for x in result.splitlines()]
        logging.info(nuvo_pod_list)

        for container in CLUSTERD_CONTAINERS:
            try:
                # get container log file
                logging.info("Collecting logs for container %s in %s", container, CLUSTERD)
                with open(self.args.log_dirpath + "/" +  container + "-" + \
                    CLUSTERD + ".log", "w") as outfile:
                    outfile.write(self.kubectl_helper.run_kubectl_logs_cmd(CLUSTERD, container_name=container, namespace=NUVOLOSO_CLUSTER))

                # get previous logs in case container restarted
                logging.info("Collecting previous logs for container %s in %s", container, CLUSTERD)
                with open(self.args.log_dirpath + "/" +  container + "-" + \
                    CLUSTERD + "-previous.log", "w") as outfile:
                    outfile.write(self.kubectl_helper.run_kubectl_logs_cmd(CLUSTERD, container_name=container, namespace=NUVOLOSO_CLUSTER, cmd_params="-p"))
            except subprocess.CalledProcessError as err:
                if err.output:
                    logging.error(err.output)
                if "previous terminated container" in err.output and \
                    "not found" in err.output:
                    logging.error("Container: %s in pod: %s has no previous "
                                  "container logs. Will move ahead to collect "
                                  "other logs", container, "clusterd")
                else:
                    logging.error("Failed to collect logs for pod: %s "
                                  "container: %s . Will move ahead to collect "
                                  "other logs", "clusterd", container)

        # list /var/crash on each node
        # For each node (except 'clusterd') get logs of agentd and nuvo
        # skip 0th since we just collected its logs (clusterd-0)
        for i in range(1, len(nuvo_pod_list)):
            for container in NUVO_CONTAINERS:
                try:
                    # get container log file
                    logging.info("Collecting logs for container %s in %s ", container, nuvo_pod_list[i])
                    with open(self.args.log_dirpath + "/" +  container + "-" + \
                        nuvo_pod_list[i] + ".log", "w") as outfile:
                        outfile.write(self.kubectl_helper.run_kubectl_logs_cmd(nuvo_pod_list[i], container_name=container, namespace=NUVOLOSO_CLUSTER))

                    # get previous logs in case container restarted
                    logging.info("Collecting previous logs for container %s in %s ", container, nuvo_pod_list[i])
                    with open(self.args.log_dirpath + "/" +  container + "-" + \
                        nuvo_pod_list[i] + "-previous.log", "w") as outfile:
                        outfile.write(self.kubectl_helper.run_kubectl_logs_cmd(nuvo_pod_list[i], container_name=container, namespace=NUVOLOSO_CLUSTER, cmd_params='-p'))

                    # ls -lrt /var/crash for nuvo containers
                    if container == "nuvo":
                        logging.info("Checking for presence of core dumps in /var/crash")
                        result = self.kubectl_helper.run_kubectl_exec_cmd("ls -lrt /var/crash", nuvo_pod_list[i], container_name="nuvo", namespace=NUVOLOSO_CLUSTER)
                        if result:
                            logging.info(result)
                except subprocess.CalledProcessError as err:
                    if err.output:
                        logging.error(err.output)
                    if "previous terminated container" in err.output and \
                        "not found" in err.output:
                        logging.error("Container: %s in pod: %s has no previous "
                                      "container logs. Will move ahead to collect "
                                      "other logs", container, nuvo_pod_list[i])
                    else:
                        logging.error("Failed to collect logs for pod: %s "
                                      "container: %s . Will move ahead to collect "
                                      "other logs", nuvo_pod_list[i], container)
        logging.info("Done collecting logs.")

    def slurp_dataplane_logs(self):
        """Collect clusterd, CSI, nuvo, agentd logs from all the pods in dataplane"""

        result = self.kubectl_helper.get_pods(namespace=NUVOLOSO_CLUSTER, output='name')
        if not result:
            raise Exception("Failed to get pods for namespace %s. Can't get logs. Exiting.." % NUVOLOSO_CLUSTER)
        logging.info(result)
        nuvo_pod_list = [x.split('/')[-1] for x in result.splitlines()]
        logging.info(nuvo_pod_list)

        # Get logs for clusterd containers
        self.args.pod = 'clusterd-0'
        self.args.namespace = 'nuvoloso-cluster'
        for container in CLUSTERD_CONTAINERS:
            logging.info('Getting logs for container %s', container)
            try:
                self.args.container = container
                logging.info("Getting logs for %s", self.args.container)

                self.args.previous = False
                private_host, c_id = self.get_host_and_container(self.args)
                public_host = self.get_aws_host(private_host)
                self.slurp_logs(public_host, c_id, self.args.pod, self.args.container)

                # previous
                self.args.previous = True
                private_host, c_id = self.get_host_and_container(self.args)
                if private_host != '':
                    logging.info('Feching previous logs for %s', container)
                    public_host = self.get_aws_host(private_host)
                    self.slurp_logs(public_host, c_id, self.args.pod, self.args.container)
            except subprocess.CalledProcessError as err:
                if err.output:
                    logging.error(err.output)
                if "previous terminated container" in err.output and \
                    "not found" in err.output:
                    logging.error("Container: %s in pod: %s has no previous "
                                  "container logs. Will move ahead to collect "
                                  "other logs", container, "clusterd")
                else:
                    logging.error("Failed to collect logs for pod: %s "
                                  "container: %s . Will move ahead to collect "
                                  "other logs", "clusterd", container)

        # Logs and crash files for agents/nuvo
        # list /var/crash on each node
        # For each node (except 'clusterd') get logs of agentd and nuvo
        # skip 0th since we just collected its logs (clusterd-0)
        for i in range(1, len(nuvo_pod_list)):
            self.args.pod = nuvo_pod_list[i]
            for j, container in enumerate(NUVO_CONTAINERS):
                try:
                    self.args.container = container
                    logging.info("Getting logs for %s", self.args.container)

                    self.args.previous = False
                    private_host, c_id = self.get_host_and_container(self.args)
                    public_host = self.get_aws_host(private_host)
                    self.slurp_logs(public_host, c_id, self.args.pod, self.args.container)

                    # previous
                    self.args.previous = True
                    private_host, c_id = self.get_host_and_container(self.args)
                    if private_host != '':
                        logging.info('Feching previous logs for %s', container)
                        public_host = self.get_aws_host(private_host)
                        self.slurp_logs(public_host, c_id, self.args.pod, self.args.container)

                    # ls -lrt /var/crash for nuvo containers
                    if NUVO_CONTAINERS[j] == "nuvo":
                        logging.info("Checking for presence of core dumps in /var/crash")
                        result = self.kubectl_helper.run_kubectl_exec_cmd("ls -lrt /var/crash", nuvo_pod_list[i], container_name="nuvo", namespace=NUVOLOSO_CLUSTER)
                        if result:
                            logging.info(result)
                except subprocess.CalledProcessError as err:
                    if err.output:
                        logging.error(err.output)
                    if "previous terminated container" in err.output and \
                        "not found" in err.output:
                        logging.error("Container: %s in pod: %s has no previous "
                                      "container logs. Will move ahead to collect "
                                      "other logs", NUVO_CONTAINERS[j], nuvo_pod_list[i])
                    else:
                        logging.error("Failed to collect logs for pod: %s "
                                      "container: %s . Will move ahead to collect "
                                      "other logs", nuvo_pod_list[i], NUVO_CONTAINERS[j])
        logging.info("Done collecting logs.")

    def collect_nuvo_cores(self):
        """Collect nuvo core"""

        logging.info("Collecting nuvo cores, if present")
        # get private ip of host vms 'kubectl describe'
        private_ip_list = self.kubectl_helper.get_private_ips()
        if not private_ip_list:
            raise Exception("No private ips (of host Vms) found")
        logging.info("private_ip_list: %s", private_ip_list)

        # get public ip of the host vms (of nuvo container)
        public_ip_list = self.aws.get_public_ips(private_ip_list)
        if not public_ip_list:
            raise Exception("No public ips (of host Vms) found for "
                            "private_ip_list: %s" % private_ip_list)
        for public_ip in public_ip_list:
            logging.info("public_ip of host VM (of nuvo): %s", public_ip)

            # list the cores reverse sorted by time (oldest core will be first)
            cmd = "ls /var/crash/"
            list_output = self.connect_ssh.execute_cmd_remote(public_ip, cmd)
            logging.info(list_output)
            if not list_output:
                logging.info("No nuvo crashes on public_ip: %s", public_ip)
                continue
            nuvo_corefiles = list_output.strip().split()
            logging.info("nuvo_corefiles: %s", nuvo_corefiles)
            i = 0
            for core_file_path in nuvo_corefiles:

                # change permissions of core file
                core_file_path = "/var/crash/" + core_file_path
                cmd = "sudo chmod 666 " + core_file_path
                logging.info("cmd: %s", cmd)
                output = self.connect_ssh.execute_cmd_remote(public_ip, cmd)
                if output:
                    logging.info(output)

                # if the core file is already compressed don't
                # compress again
                if core_file_path[-3:] == ".gz":
                    logging.info("core file: %s is already compressed. "
                                 "Skipping gzip..", core_file_path)
                else:
                    # compress the core file
                    cmd = "sudo gzip " + core_file_path
                    logging.info("cmd: %s", cmd)
                    output = self.connect_ssh.execute_cmd_remote(public_ip, cmd)
                    if output:
                        logging.info(output)
                    core_file_path = core_file_path + ".gz"

                # scp the file over
                self.connect_ssh.scp_copy_file(public_ip, src_path=core_file_path,
                                               dst_path=self.args.log_dirpath)

                i = i + 1
                if i == MAX_NUM_CORES_COLLECTED:
                    logging.info("Done collecting max number of cores. "
                                 "(MAX_NUM_CORES_COLLECTED: %d)", MAX_NUM_CORES_COLLECTED)
                    break
            cmd = "ls -lrt " + self.args.log_dirpath
            result = subprocess.check_output(
                cmd,
                stderr=subprocess.STDOUT,
                timeout=CHECK_OUTPUT_TIMEOUT,
                encoding='UTF-8',
                shell=True)
            if result:
                logging.info(result)
            logging.info("cmd: %s", cmd)
            self.nuvoloso_helper.run_check_output(cmd)

    def collect_progress_files(self):
        """Collect progress files"""

        result = self.kubectl_helper.get_pods(namespace=NUVOLOSO_CLUSTER, output='name')
        if not result:
            raise Exception("Failed to get pods for namespace %s. Can't get logs. Exiting.." % NUVOLOSO_CLUSTER)
        logging.info(result)
        nuvo_pod_list = [x.split('/')[-1] for x in result.splitlines()]
        logging.info(nuvo_pod_list)

        for pod in nuvo_pod_list:
            try:
                list_output = self.kubectl_helper.run_kubectl_exec_cmd("bash -c \"ls /tmp/*_progress*\"", pod, container_name="agentd", namespace=NUVOLOSO_CLUSTER)
                progress_files = list_output.strip().split()

                for progress_file in progress_files:
                    logging.info('Getting progress file %s', progress_file)
                    dir_name = '%s/%s/%s' % (self.args.log_dirpath, pod, "progress_files")
                    cmd = ['mkdir', '-p', dir_name]
                    retcode = subprocess.call(cmd)
                    if retcode:
                        sys.exit(1)
                    dest_progress = dir_name + "/" + progress_file.strip('/tmp')
                    logging.info("Executing copy command to get: %s", progress_file)
                    result = self.kubectl_helper.get_file_from_cluster(progress_file, dest_progress, pod, container_name="agentd", namespace=NUVOLOSO_CLUSTER)
                    logging.info("Output from copy command is: %s", result)

            except subprocess.CalledProcessError as err:
                if err.output:
                    logging.error(err.output)
        logging.info("Done collecting progress files.")



    def collect_nuvo_binary(self):
        """Copies nuvo binary from any one of the nuvo containers out to log_dirpath"""

        logging.info("Collecting nuvo binary..")
        try:
            # We need only one nuvo pod since all pods have same nuvo binary
            nuvo_pod = self.get_dataplane_pods(skip_clusterd=True)[0]
            result = self.kubectl_helper.get_file_from_cluster(PATH_NUVO_BINARY, self.args.log_dirpath,
                                                        nuvo_pod, container_name=NUVO_CONTAINER, namespace=NUVOLOSO_CLUSTER )
            if result:
                logging.info(result)
            cmd = "ls -lrt " + self.args.log_dirpath
            logging.info("cmd: %s", cmd)
            result = self.nuvoloso_helper.run_check_output(cmd)
            if result:
                logging.info(result)
        except subprocess.CalledProcessError as err:
            if err.output:
                logging.error(err.output)
            logging.error("WARNING: Failed to collect nuvo binary. Moving on..")
        except:
            logging.error("WARNING: Failed to collect nuvo binary. Moving on..")
            raise

    def plane_exists(self, get_pods_cmd):
        """Returns true if namespace exists, false otherwise"""

        try:
            self.nuvoloso_helper.run_check_output(get_pods_cmd)
            return True
        except subprocess.CalledProcessError as err:
            if err.output:
                logging.error(err.output)
            if "did you specify the right host" in err.output:
                return False
            else:
                raise

    def collect_support_bundle(self):
        """Collect logs & cores from data plane"""

        InstallPackages.apt_get_update()
        InstallPackages.install_kops()
        InstallPackages.install_kubectl()
        InstallPackages.install_awscli()
        InstallPackages.configure_aws(self.args)
        InstallPackages.generate_sshkeypair()
        InstallPackages.install_pip3()
        InstallPackages.install_paramiko()
        InstallPackages.install_scp()
        InstallPackages.install_boto3()

        time.sleep(10)

        # For each context, try and fetch all kontroller logs
        context_list = self.kubectl_helper.get_all_contexts()

        logging.info(context_list)
        for context in context_list:
            self.kubectl_helper.context = context

            logging.info('Pulling logs from context %s', context)

            pod_list = self.kubectl_helper.get_pods(namespace=NUVOLOSO_CLUSTER, output='name')
            if pod_list:
                logging.info("==================")
                logging.info("Kubectl cluster for dataplane found. "
                             "Collecting logs/cores now..")
                self.collect_progress_files()
                self.collect_container_status()
                self.slurp_dataplane_logs()
                self.collect_nuvo_cores()
                self.collect_nuvo_binary()
            else:
                logging.info("No dataplane pods to retrieve logs from in context %s", context)

            # collect control plane's logs
            pod_list = self.kubectl_helper.get_pods(namespace=NUVOLOSO_MANAGEMENT, output='name')

            if pod_list:
                logging.info("==================")
                logging.info("Kubectl cluster for control plane found. "
                             "Collecting logs now..")
                self.collect_container_status(NUVOLOSO_MANAGEMENT)
                self.slurp_controlplane_logs()
                logging.info("==================")
            else:
                logging.info("No control plane pods to retrieve logs from in context %s", context)


    def container_status(self, pod, container_name):
        """Find the status of the named container within the pod status.
        """
        pod_name = pod['metadata']['name']
        statuses = pod['status']['containerStatuses']
        if 'initContainerStatuses' in pod['status']:
            statuses.extend(pod['status']['initContainerStatuses'])
        if not container_name:
            if len(statuses) > 1:
                logging.error('a container name must be specified for pod %s', pod_name)
                sys.exit(1)
            status = statuses[0]
        else:
            for status in statuses:
                if status['name'] == container_name:
                    return status
            logging.error('container %s is not valid for pod %s', container_name, pod_name)
            sys.exit(1)
        return status


    def get_host_and_container(self, args):
        """use kubectl to determine the (private) hostname and container ID.
        """
        data = ''
        try:
            logging.info("Executing 'get pods' for pod %s in namespace %s", args.pod, args.namespace)
            data = self.kubectl_helper.get_pods(pod_name=args.pod, namespace=args.namespace, output='json')
            pod = json.loads(data)
        except subprocess.CalledProcessError as exc:
            data = exc.output
            if data:
                print(data)
            sys.exit(1)
        except ValueError as exc:
            #logging.error('%s did not output valid JSON', cmd)
            logging.error(data)
            sys.exit(1)

        status = self.container_status(pod, args.container)
        if args.previous:
            if 'lastState' in status and 'terminated' in status['lastState']:
                c_id = status['lastState']['terminated']['containerID']
            else:
                logging.info('container %s has no previous state', status['name'])
                return '', ''
        else:
            c_id = status['containerID']

        return pod['spec']['nodeName'], c_id.split('//', 1)[1]


    def get_aws_host(self, k8s_host):
        """Given the private name of an AWS host, returns its public name.
        """
        client = boto3.client('ec2', region_name=DEFAULT_REGION)
        response = client.describe_instances()
        for res in response['Reservations']:
            for i in res['Instances']:
                if i['PrivateDnsName'] == k8s_host:
                    return i['PublicDnsName']
        print('cannot find instance with private name', k8s_host)
        sys.exit(1)


    def slurp_logs(self, aws_host, c_id, pod, container):
        """Downloads all of the available logs of one container from a kubernetes kops cluster
        into a subdirectory with the path ./${podname}/${container}.  The subdirectory will
        be created when the script is run.  Multiple containers from the same pod
        will share the same parent ${podname} directory.
        """

        # the logs are readable only by root, copy, chown and chmod them so they can be downloaded
        tmp_log_dir = "docker_logs_%s" % os.getpid()
        cmd = 'sudo sh -c "mkdir -p %s && cp /var/lib/docker/containers/%s/%s-json.log* %s/" && ' \
            'sudo sh -c "chown %s %s/%s-json.log* && chmod 600 %s/%s-json.log*"' % \
            (tmp_log_dir, c_id, c_id, tmp_log_dir, DEFAULT_LOGIN, tmp_log_dir, c_id, tmp_log_dir, c_id)

        logging.info('About to execute %s on host %s', cmd, aws_host)
        cmd_output = self.connect_ssh.execute_cmd_remote(aws_host, cmd)
        logging.info('Output is %s', cmd_output)

        if self.args.previous:
            dir_name = '%s/%s/%s-previous' % (self.args.log_dirpath, pod, container)
        else:
            dir_name = '%s/%s/%s' % (self.args.log_dirpath, pod, container)
        cmd = ['mkdir', '-p', dir_name]
        retcode = subprocess.call(cmd)
        if retcode:
            sys.exit(1)

        # scp prints status of the the copy, so no need for additional messages on
        # success or failure
        remote_file_list = '%s/%s-json.log*' % (tmp_log_dir, c_id)
        logging.info('About to scp files from host %s: %s to %s',
                     aws_host, remote_file_list, dir_name)
        self.connect_ssh.scp_copy_file(aws_host, remote_file_list, dir_name)
        logging.info('Output is %s', cmd_output)

        # delete the copy
        cmd = 'sudo sh -c "rm -rf %s"' % tmp_log_dir
        logging.info('About to execute %s on host %s', cmd, aws_host)
        cmd_output = self.connect_ssh.execute_cmd_remote(aws_host, cmd)
        logging.info('Output is %s', cmd_output)



def main():
    '''main'''

    default_aws_access_key = default_aws_secret_access_key = default_kops_state_store = None

    if os.environ.get('KOPS_STATE_STORE') is None:
        default_kops_state_store = os.environ.get('KOPS_STATE_STORE')

    if os.environ.get('AWS_ACCESS_KEY'):
        default_aws_access_key = os.environ.get('AWS_ACCESS_KEY')

    if os.environ.get('AWS_SECRET_ACCESS_KEY') is None:
        default_aws_secret_access_key = os.environ.get('AWS_SECRET_ACCESS_KEY')

    parser = argparse.ArgumentParser(description="Collects logs and cores "
                                     "from data plane")
    parser.add_argument(
        '--nuvo_kontroller_hostname', help='Hostname of https svc of Nuvo Kontroller',
        required=True)
    parser.add_argument(
        '--k8s_master_zone', help='aws zone for master node [default=us-west-2c]',
        default='us-west-2c')
    parser.add_argument(
        '--kops_state_store', help='state store for cluster',
        default=default_kops_state_store)
    parser.add_argument(
        '--aws_access_key', help='aws AccessKey', default=default_aws_access_key)
    parser.add_argument(
        '--aws_secret_access_key', help='aws SecretAccessKey',
        default=default_aws_secret_access_key)
    parser.add_argument(
        '--region', help='aws region [default=us-west-2]', default='us-west-2')
    parser.add_argument(
        '--account_name', help='Nuvoloso account name', default='Demo Tenant')
    parser.add_argument(
        '--log_dirpath', help='log dir to hold test and nuvo logs', default=None)
    parser.add_argument(
        '--aws_profile_name', help='aws profile name from shared credentials file', default=None)

    args = parser.parse_args()
    assert(args.kops_state_store and args.aws_access_key
           and args.aws_secret_access_key), "Some/all input parameters are not filled. Aborting"
    home_dir = os.environ.get('HOME') if os.environ.get('HOME') else "~"
    args.log_dirpath = args.log_dirpath if args.log_dirpath else home_dir + "/logs-" + \
            str(uuid.uuid4())[:5]
    pathlib.Path(args.log_dirpath).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(format='%(asctime)s %(message)s', filename=args.log_dirpath + "/" +
                        os.path.basename(__file__) + ".log", level=logging.INFO)
    print("Script to collect logs & nuvo core files from dataplane")
    tool = CollectSupportBundle(args)
    tool.collect_support_bundle()

if __name__ == '__main__':
    main()
