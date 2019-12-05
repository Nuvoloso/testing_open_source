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

Contains methods for kubectl commands
"""

import datetime
import logging
import time
import subprocess
import yaml
import json
from shlex import quote
from nuvoloso.nuvoloso.nuvoloso_helper import NuvolosoHelper

NUVO_CONTAINERS = ["agentd", "nuvo"]
DEFAULT_LABEL_KEY = "nodekey"
DEFAULT_LABEL_VALUE = "nodevalue"
NUVO_NAMESPACE = "nuvoloso-cluster"
KON_NAMESPACE = "nuvoloso-management"
KUBECTL_CREATE = "kubectl create --context %s"
KUBECTL_CREATE_DEPLOYMENT = "kubectl create --context %s -f"
KUBECTL_DELETE = "kubectl delete --context %s"
KUBECTL_DELETE_DEPLOYMENT = "kubectl delete --context %s -f"
KUBECTL_DELETE_POD = "kubectl delete pod --context %s"
KUBECTL_EXEC = "kubectl exec -i %s -c %s --context %s"
KUBECTL_EXEC_NUVO = "kubectl exec --context %s -c nuvo -n " + NUVO_NAMESPACE
KUBECTL_GET_PODS = "kubectl get pods %s --context %s"
KUBECTL_GET_PODS_WIDE = "kubectl get pods --output=wide --context %s"
KUBECTL_GET_PODS_NUVO = "kubectl get pods --context %s -n " + NUVO_NAMESPACE
KUBECTL_DESCRIBE_PODS = "kubectl describe pods --context %s"
KUBECTL_DESCRIBE_PODS_NUVO = "kubectl describe pods --context %s -n " + NUVO_NAMESPACE
KUBECTL_DESCRIBE_NODES = "kubectl describe nodes --context %s"
KUBECTL_GET_PV = "kubectl get pv %s --context %s"
KUBECTL_GET_PVC = "kubectl get pvc %s --context %s"
KUBECTL_DELETE_PVC = "kubectl delete pvc %s --context %s"
KUBECTL_LABEL = "kubectl label nodes --context %s"
KUBECTL_LOGS = "kubectl logs %s --context %s"
KUBECTL_GET_CONTEXTS = "kubectl config get-contexts"
KUBECTL_CURRENT_CONTEXT = "kubectl config current-context"
KUBECTL_GET_NODES = "kubectl get nodes --context %s"
KUBECTL_USE_CONTEXT = "kubectl config use-context"
K8S_VOLUME_PREFIX = "nuvoloso-volume-"
KUBECTL_APPLY = "kubectl apply --context %s -f"
KUBECTL_CP = "kubectl cp --context %s"
KUBECTL_GET_NS = "kubectl get namespace --context %s"
KUBECTL_GET_SECRET = "kubectl get secret --context %s"
WAIT_TIMEOUT = 1200
DEFAULT_NAMESPACE = 'default'
CONTAINER_AGENTD = "agentd"
REI_ARENA = "/var/run/nuvoloso/rei"
SERVICES_POD = "services-0"
CENTRALD = "centrald"

logger = logging.getLogger(__name__)


class KubectlHelper(object):
    """Contains methods for kubectl commands"""

    def __init__(self, args, context=None):
        self.nuvoloso_helper = NuvolosoHelper(args)
        self.context = context if context else self.get_current_context()


    def get_current_context(self):
        return self.nuvoloso_helper.run_check_output(KUBECTL_CURRENT_CONTEXT)


    def get_all_contexts(self):
        cmd = KUBECTL_GET_CONTEXTS + " -o name"
        context_list = self.nuvoloso_helper.run_check_output(cmd)
        assert context_list, "No contexts found."
        return context_list.split('\n')


    def kubectl_switch_context(self, switch_to_cluster=None):
        """Switches context between two clusters. Expects only two clusters in the setup"""

        # get all clusters in the context
        output = self.nuvoloso_helper.run_check_output(KUBECTL_GET_CONTEXTS)
        if output: logger.info(output)
        # get the current context (e.g. nuvodata.k8s.local)
        current_cluster = self.context
        logger.info("Before switching - Cluster with current-context: %s", current_cluster)
        # Switch to the other cluster (e.g. nuvokon.k8s.local)
        # This tool expects to see only two cluster: one for Nuvoloso management plane and
        # another for Nuvoloso dataplane. We expect to be switching only between these two
        # clusters. So here, we filter out the current cluster, from the output
        if not switch_to_cluster:
            cmd = "%s | awk 'FNR == 1 {next} {print $2}' | grep -v %s" % (KUBECTL_GET_CONTEXTS, current_cluster)
            switch_to_cluster = self.nuvoloso_helper.run_check_output(cmd)
            if not switch_to_cluster:
                raise Exception("Cmd: %s yielded no output" % cmd)
        # switch context
        if current_cluster != switch_to_cluster:
            cmd = "%s %s" % (KUBECTL_USE_CONTEXT, switch_to_cluster)
            logger.info("cmd: %s", cmd)
            for i in range(5):
                try:
                    output = self.nuvoloso_helper.run_check_output(cmd)
                except subprocess.CalledProcessError as err:
                    # Retry
                    time.sleep(5)
                    continue
                break

            if not output:
                raise Exception("Cmd: %s yielded no output" % cmd)
            if "Switched" not in output and switch_to_cluster not in output:
                raise Exception("Failed to switch context to %s" % switch_to_cluster)
            new_current_cluster = self.get_current_context()
            if not new_current_cluster:
                raise Exception("Cmd: %s yielded no output" % KUBECTL_CURRENT_CONTEXT)
            logger.info("After switching - Cluster with current-context: %s", new_current_cluster)
            self.context = new_current_cluster
        else:
             logger.info("Already using %s", current_cluster)


    def get_mgmt_context(self):
        cur_context = self.context
        contexts = self.get_all_contexts()
        for context in contexts:
            self.context = context
            if KON_NAMESPACE in self.get_all_namespaces():
                self.context = cur_context
                return context
        logger.error("No mgmt context found in %s", contexts)
        raise RuntimeError("No mgmt context found")


    def get_private_ips(self):
        """Parses kubectl describe and gets the private ip address of hosts VM"""

        cmd = "%s | grep 'Node:'" % (KUBECTL_DESCRIBE_PODS_NUVO % self.context)
        logger.info("Running cmd: %s", cmd)
        output = self.nuvoloso_helper.run_check_output(cmd)
        if not output:
            raise Exception("Cmd: %s yielded no output" % cmd)
        output.strip()
        logger.info(output)
        private_ip_list = set()
        output_lines = output.split("\n")
        for items_ips in output_lines:
            if "compute.internal" in items_ips:
                private_ip_list.add(items_ips.split("/")[1])
        return list(private_ip_list)


    def run_kubectl_exec_cmd(self, cmd, pod_name, container_name, namespace=DEFAULT_NAMESPACE):
        """runs kubectl exec command inside a container"""

        cmd = "%s -n %s -- %s" % (KUBECTL_EXEC % (pod_name, container_name, self.context), namespace, cmd)
        #logger.info("cmd: %s", cmd)
        result = self.nuvoloso_helper.run_check_output(cmd)
        if result:
            logger.info(result)
            return result


    def check_pod_running(self, pod_name, namespace=DEFAULT_NAMESPACE):
        """Returns True if pod is in running state, False otherwise"""

        cmd_succeeded = False
        cmd = "%s -n %s" % (KUBECTL_GET_PODS % (pod_name, self.context), namespace)
        result = self.nuvoloso_helper.run_check_output(cmd)
        if result:
            logger.info(result)
            #TODO: Parse each line to check for no-restarts and running.
            if result.count("Running") == 1:
                logger.info("Container: %s is in 'Running' state", pod_name)
                cmd_succeeded = True
        else:
            raise Exception("Command -' %s ' "
                            " did not yield any response" % cmd)
        return cmd_succeeded


    def check_pod_completed(self, pod_name, namespace=DEFAULT_NAMESPACE):
        """Returns True if pod is in complete state, False otherwise"""

        cmd_succeeded = False
        cmd = "%s -n %s" % (KUBECTL_GET_PODS % (pod_name, self.context), namespace)
        result = self.nuvoloso_helper.run_check_output(cmd)
        if result:
            logger.info(result)
            #TODO: Parse each line to check for no-restarts and running.
            if result.count("Completed") == 1:
                logger.info("Container: %s is in 'Completed' state", pod_name)
                cmd_succeeded = True
        else:
            raise Exception("Command -' %s ' "
                            " did not yield any response" % cmd)
        return cmd_succeeded


    def wait_for_app_container(self, pod_name, namespace=DEFAULT_NAMESPACE):
        """Waits for app container to go to 'Running' state"""

        time_now = datetime.datetime.utcnow()
        cmd_succeeded = False
        while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(seconds=WAIT_TIMEOUT)):
            time.sleep(5)
            pod_running = self.check_pod_running(pod_name, namespace=namespace)
            if pod_running:
                logger.info("Pod %s in namespace (%s) is running fine", pod_name, namespace)
                cmd_succeeded = True
                time.sleep(10)
                break
            else:
                logger.info("Pod %s (in %s namespace) is not ready yet. "
                             "Sleeping for 5 seconds..", pod_name, namespace)
        if not cmd_succeeded:
            raise Exception("TIMEOUT (%s seconds) waiting for app cluster to"
                            "  go to 'Running' state." % WAIT_TIMEOUT)
        return cmd_succeeded


    def deploy_appcontainer(self, app_yaml_path, volume_name, pod_name, pvc_name,
                            iteration_number=None, dynamic=True, vol_id=None, namespace=DEFAULT_NAMESPACE):
        """Deploys a container for user-app (fio) with Nuvo volume"""

        # deploy app & claim pv
        logger.info("Deploy user-app yaml file: %s to create a "
                     "container using nuvo-vol volume: %s ", app_yaml_path, volume_name)
        try:
            cmd = "%s %s" % (KUBECTL_CREATE_DEPLOYMENT % (self.context), app_yaml_path)
            result = self.nuvoloso_helper.run_check_output(cmd)
            if result:
                logger.info(result)
            if self.wait_for_app_container(pod_name=pod_name, namespace=namespace):
                if dynamic:
                    time_now = datetime.datetime.utcnow()
                    cmd_succeeded = False
                    while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(seconds=WAIT_TIMEOUT)):
                        result = self.get_pvc(pvc_name=pvc_name, output='yaml', namespace=namespace)
                        if result:
                            logger.info(result)
                            data = yaml.load(result)
                            if data['metadata']['name'] == pvc_name and data['status']['phase'] == "Bound":
                                logger.info("PVC: %s was created in Kubernetes", pvc_name)
                                cmd_succeeded = True
                                volume_name = data['spec']['volumeName']
                                time.sleep(5)
                                break
                            else:
                                logger.info('PVC is not ready yet.  Sleeping for 60 seconds...')
                                time.sleep(60)
                        else:
                            raise Exception('kubectl get pvc failed')

                    if not cmd_succeeded:
                            raise Exception("TIMEOUT (%s seconds) waiting for pvc to complete" % WAIT_TIMEOUT)
                else:
                    result = self.get_pv(pv_name=K8S_VOLUME_PREFIX + vol_id)
                    if result:
                        logger.info(result)
                    if vol_id in result and pvc_name in result and (result.count("Bound") == 1):
                        logger.info("SUCCESS - nuvo-volume with matcher_ud: %s mounted fine "
                                    " inside app container", pvc_name)
                    else:
                        raise Exception("nuvo-volume's matcher id not found in result")
            else:
                raise Exception("App container Failed to deploy")
        except subprocess.CalledProcessError as err:
            if err.output:
                logger.info(err.output)
            result = self.describe_pods()
            if result:
                logger.info(result)
            result = self.get_pods(namespace=NUVO_NAMESPACE)
            if result:
                logger.info(result)
            result = self.get_pv()
            if result:
                logger.info(result)
            logger.info("Failed to deploy app cluster/namespace")
            raise


    def run_kubectl_logs_cmd(self, pod_name, container_name=None, cmd_params="", namespace=DEFAULT_NAMESPACE):
        """runs kubectl logs cmd for a container in default namespace"""

        # cmd_params can be used to pass in "-n nuvoloso-cluster"
        cmd = "%s -n %s" % (KUBECTL_LOGS % (pod_name, self.context), namespace)
        if container_name:
            cmd += " -c %s " % container_name
        cmd += cmd_params
        logger.info("cmd: %s", cmd)
        result = self.nuvoloso_helper.run_check_output(cmd)
        if result:
            logger.info(result)
            return result
        else:
            raise Exception("No output when running cmd: %s" % cmd)


    def run_kubectl_delete_cmd(self, path_to_yaml):
        """runs kubectl delete command to delete a pod"""

        cmd = "%s %s" % (KUBECTL_DELETE_DEPLOYMENT % (self.context), path_to_yaml)
        logger.info("cmd: %s", cmd)
        result = self.nuvoloso_helper.run_check_output(cmd)
        if result:
            logger.info(result)
            return result
        else:
            raise Exception("No output when running cmd: %s" % cmd)


    def get_nodes_cluster(self, skip_master_node=True):
        """Runs kubectl get nodes and grabs the names of all slave nodes"""

        node_list = []
        cmd = "%s | awk 'FNR == 1 {next} {print $1\":\"$3}'" % KUBECTL_GET_NODES % self.context
        if skip_master_node:
            cmd = cmd + " | grep -v 'master'"
        logger.info("cmd: %s", cmd)
        result = self.nuvoloso_helper.run_check_output(cmd)
        if result:
            logger.info(result)
            node_list_raw = result.split()
            # e.g. node_list_raw = ['ip-172-20-47-154.us-west-2.compute.internal:node']
            # remove ":node" from each line of node_list_raw and get the hostname
            node_list = list(map(lambda x:x.split(":")[0], node_list_raw))
            return node_list
        else:
            raise Exception("No output when running cmd: %s" % cmd)


    def label_nodes(self, node_name, label_key="", label_value=""):
        """Labels the nodes in the cluster"""

        label_key = label_key if label_key else DEFAULT_LABEL_KEY
        label_value = label_value if label_value else DEFAULT_LABEL_VALUE
        if not label_key or not label_value:
            raise Exception("label_key and label_value are required for labeling the node")
        cmd = "%s %s %s=%s --overwrite" % (KUBECTL_LABEL % self.context, node_name, label_key, label_value)
        logger.info("cmd: %s", cmd)
        result = self.nuvoloso_helper.run_check_output(cmd)
        if result:
            logger.info(result)
        if not result or "labeled" not in result:
            raise Exception("No/unexpected output when running cmd: %s" % cmd)
        return result


    def check_pod_node_assignment(self, pod_name, node_name):
        """Returns True if pod is running the on given node_name. False otherwise"""

        cmd = KUBECTL_GET_PODS_WIDE % self.context
        logger.info("cmd: %s", cmd)
        result = self.nuvoloso_helper.run_check_output(cmd)
        if result: logger.info(result)

        cmd = "%s %s" % (KUBECTL_GET_PODS_WIDE % (self.context), pod_name)
        logger.info("cmd: %s", cmd)
        result = self.nuvoloso_helper.run_check_output(cmd)
        if result: logger.info(result)
        if not result:
            raise Exception("No output when running cmd: %s" % cmd)
        cmd = cmd + " | awk 'FNR == 1 {next} {print $7}'"
        logger.info("cmd: %s", cmd)
        result = self.nuvoloso_helper.run_check_output(cmd)
        if result:
            logger.info(result)
        if not result:
            raise Exception("No output when running cmd: %s" % cmd)
        if node_name in result:
            return True
        else:
            return False


    def get_node_external_dns(self, label_name, node_label):
        """ Returns the External DNS name for a node with a specific label"""

        cmd = "%s -l %s=%s | grep ExternalDNS:" % (KUBECTL_DESCRIBE_NODES % self.context, label_name, node_label)
        result = self.nuvoloso_helper.run_check_output(cmd)
        assert result, "No output when running cmd: %s" % cmd
        return result.split()[-1]


    def get_scheduled_node(self, pod_name, namespace=DEFAULT_NAMESPACE):
        """ Returns then node name where the specified pod is scheduled """

        cmd = "%s %s -n %s| grep Node:" % (KUBECTL_DESCRIBE_PODS % self.context, pod_name, namespace)
        result = self.nuvoloso_helper.run_check_output(cmd)
        assert result, "No output when running cmd: %s" % cmd
        return result.split()[-1].split('/')[0]


    def get_node_label(self, label_name, node_name):
        """ Returns value of the specified node label of a given node. """

        cmd = "%s %s| grep %s" % (KUBECTL_DESCRIBE_NODES % self.context, node_name, label_name)
        result = self.nuvoloso_helper.run_check_output(cmd)
        assert result, "No output when running cmd: %s" % cmd
        return result.split('=')[-1].strip()


    def deploy(self, deployment_yaml):
        """ Creates/updates a new or existing deployment """

        cmd = "%s %s" % (KUBECTL_APPLY % (self.context), deployment_yaml)
        result = self.nuvoloso_helper.run_check_output(cmd)
        return result


    def get_pvc(self, pvc_name='', output=None, namespace=DEFAULT_NAMESPACE):
        """ Returns details of a Persistent Volume Claim """

        cmd = "%s -n %s" % (KUBECTL_GET_PVC % (pvc_name, self.context), namespace)
        if output:
            cmd += " -o %s" % output
        result = self.nuvoloso_helper.run_check_output(cmd)
        return result


    def delete_pvc(self, pvc_name, namespace=DEFAULT_NAMESPACE):
        """ Deletes the specified pvc """

        cmd = "%s -n %s" % (KUBECTL_DELETE_PVC % (pvc_name, self.context), namespace)
        result = self.nuvoloso_helper.run_check_output(cmd)
        return result


    def get_pods(self, pod_name='', namespace=DEFAULT_NAMESPACE, label=None, output=None):
        """ Returns the list of pods in a namespace, if specified """

        cmd = "%s -n %s" % (KUBECTL_GET_PODS % (pod_name, self.context), namespace)
        if output:
            cmd += " -o %s" % output
        if label:
            cmd += " -L %s" % label
        result = self.nuvoloso_helper.run_check_output(cmd)
        return result


    def delete_pod(self, pod_name, namespace=DEFAULT_NAMESPACE):
        """ Deletes pod from a namespace """

        cmd = "%s %s -n %s" % (KUBECTL_DELETE_POD % (self.context), pod_name, namespace)
        result = self.nuvoloso_helper.run_check_output(cmd)
        return result


    def describe_pods(self, pod_name='', namespace=DEFAULT_NAMESPACE):
        """ Returns detailed information about one or all pods in a namespace """

        cmd = "%s -n %s %s" % (KUBECTL_DESCRIBE_PODS % (self.context), namespace, pod_name)
        result = self.nuvoloso_helper.run_check_output(cmd)
        return result


    def get_pv(self, pv_name='', output=None):
        """ Returns details of persistent volume(s) for the specified namespace """

        cmd = KUBECTL_GET_PV % (pv_name, self.context)
        if output:
            cmd += " -o %s" % output
        result = self.nuvoloso_helper.run_check_output(cmd)
        return result


    def copy_file_to_cluster(self, src, dest, pod_name, container_name=None, namespace=DEFAULT_NAMESPACE):
        """ Copies a file specified in 'src' to a file specfied in 'dest' on a specified pod """

        cmd = "%s %s %s/%s:%s" % (KUBECTL_CP % (self.context), src, namespace, pod_name, dest)
        if container_name:
            cmd += " -c %s" % container_name
        result = self.nuvoloso_helper.run_check_output(cmd)
        return result


    def get_file_from_cluster(self, src, dest, pod_name, container_name=None, namespace=DEFAULT_NAMESPACE):
        """ Copies a file specified in 'src' from a specified pod to a file specified in 'dest' """

        cmd = "%s %s/%s:%s %s" % (KUBECTL_CP % (self.context), namespace, pod_name, src, dest)
        if container_name:
            cmd += " -c %s" % container_name
        result = self.nuvoloso_helper.run_check_output(cmd)
        return result


    def create_namespace(self, namespace):
        """ creates a namespace """

        cmd = KUBECTL_CREATE % (self.context) + " namespace " + namespace
        try:
            result = self.nuvoloso_helper.run_check_output(cmd)
            return result
        except subprocess.CalledProcessError as err:
            if "AlreadyExists" in err.output:
                pass
            else:
                raise


    def get_all_namespaces(self):
        """ Return all namespaces """

        cmd = "%s -o name" % (KUBECTL_GET_NS % self.context)
        result = self.nuvoloso_helper.run_check_output(cmd)
        return result


    def rei_set(self, name, pod_name, bool_value=False, string_value='', int_value=0, float_value=0.0, num_uses=1, do_not_delete=False, \
        container_name=CONTAINER_AGENTD, namespace=NUVO_NAMESPACE, arena=REI_ARENA):
        """Inject a runtime error.\n
           name (str):           The name of the REI file relative to the arena. e.g. vreq/foo\n
           pod_name (str):       The name of the pod\n
           bool_value(bool):     A boolean value to set. Default False if no value explicitly specified, otherwise True\n
           string_value(str):    A string value to set. Default empty\n
           int_value(int):       An integer value to set. Default 0\n
           float_value(float):   A floating point value to set. Default 0.0\n
           num_uses(int):        The number of uses. Do not use with persistent\n
           do_not_delete (bool): Specify True if the error should persist. Use rei_clear to remove later\n
           arena (str):          The path of the REI arena. Default is the standard REI arena path\n
           container_name (str): The name of the container within the pod. Default is that of agentd\n
           namespace (str):      The name of the namespace. Default is that of the managed cluster\n
        """
        rei = {}
        if bool_value:
            rei["boolValue"] = True
        if int_value != 0:
            rei["intValue"] = int_value
        if string_value != '':
            rei["stringValue"] = string_value
        if float_value != 0.0:
            rei["floatValue"] = float_value
        if num_uses > 1:
            rei["numUses"] = num_uses
        if len(rei) == 0:
            rei["boolValue"] = True
        if do_not_delete:
            rei["doNotDelete"] = True
        val = json.dumps(rei)
        cmd = [quote(arg) for arg in [ 'bash', '-c', "echo '"+val+"' > "+arena+"/"+name ]]
        self.run_kubectl_exec_cmd(" ".join(cmd), pod_name, container_name, namespace)
        return val


    def rei_clear(self, name, pod_name, container_name=CONTAINER_AGENTD, namespace=NUVO_NAMESPACE, arena=REI_ARENA):
        """Remove an REI file.\n
           name (str):           The name of the REI file relative to the arena. e.g. vreq/foo\n
           pod_name (str):       The name of the pod\n
           arena (str):          The path of the REI arena. Default is the standard REI arena path.\n
           container_name (str): The name of the container within the pod. Default is that of agentd.\n
           namespace (str):      The name of the namespace. Default is that of the managed cluster.\n
        """
        cmd = ['rm', '-f', arena+"/"+name]
        self.run_kubectl_exec_cmd(" ".join(cmd), pod_name, container_name, namespace)


    def cleanup_cluster(self, cluster_name, domain_name, account_name, delete_volumes=True, fail_requests=True):
        """
        Run the cluster_delete script to remove the cluster's references from the kontroller. 
        All app clusters are expected to be already deleted by kops at this point.

        cluster_name (str):     The name of the cluster as mentioned in the kontroller.
        domain_name (str):      The csp domain name that the cluster belongs to.
        account_name (str):     The Account owner of the cluster.
        delete_volumes (bool):  To delete volumes-series associated with the cluster permanently.
        fail_requests (bool):   To mark outstanding requests to the cluster as failed.
        """

        cmd = ["/opt/nuvoloso/bin/cluster_delete.py -C %s -D %s -A '%s' -y" % (cluster_name, domain_name, account_name)]
        if delete_volumes:
            cmd.append("--delete-volumes")
        if fail_requests:
            cmd.append("--fail-requests")
        cur_context = self.context
        self.context = self.get_mgmt_context()
        self.run_kubectl_exec_cmd(' '.join(cmd), SERVICES_POD, CENTRALD, namespace=KON_NAMESPACE)
        self.context = cur_context
        return


    def get_secret(self, name=None, namespace=DEFAULT_NAMESPACE, format='json'):
        """
        Returns all or specified secret in a namespace
        name (str): secret name. By default returns all secrets
        namespace (str): Namespace name.
        format (str): Takes 'yaml', 'json' or other formats supported by -o option in kubectl
        """

        cmd = "%s -n %s -o %s " % ((KUBECTL_GET_SECRET % self.context), namespace, format)
        if name:
            cmd += name
            result = self.nuvoloso_helper.run_check_output(cmd)
            return result

    def delete_resource(self, resource_type, resource_name, namespace=DEFAULT_NAMESPACE):
        """
        Deletes the specified resource
        resource_type (str)     : Resource type
        resource_name (str)     : Name of the resource to be deleted
        namespace (str)         : The namespace where the resource runs
        """

        cmd = "%s %s %s -n %s" % ((KUBECTL_DELETE % self.context), resource_type, resource_name, namespace)
        result = self.nuvoloso_helper.run_check_output(cmd)
        return result

