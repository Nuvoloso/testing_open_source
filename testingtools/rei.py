#!/usr/bin/env python3
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

This script provides control over runtime error injection.
The targeted daemons must have REI support enabled.
"""

import argparse
import logging
import os
import pathlib

from nuvoloso.dependencies.kubectl_helper import KubectlHelper, NUVO_NAMESPACE as NS_MANAGED_CLUSTER, KON_NAMESPACE as NS_MANAGEMENT_SERVICE

AGENTD_CONTAINER = "agentd"
CENTRALD_CONTAINER = "centrald"
CENTRALD_POD = "services-0"
CLUSTERD_CONTAINER = "clusterd"
CLUSTERD_POD = "clusterd-0"
DEF_KUBECTL_CONTEXT = 'nuvotest.k8s.local'
DEF_LOG_DIR = "~/rei-log"

def main():
    """main"""
    parser = argparse.ArgumentParser(description="Manage runtime error injection")
    parser.add_argument('-n', '--rei-name', help='The name of an REI file, including its arena relative directory. e.g. vreq/foo', required=True)
    parser.add_argument('-b', '--bool-value', action='store_true', help='Set a True value. This is automatic if no values are set')
    parser.add_argument('-s', '--string-value', help='Set a string value.', default='')
    parser.add_argument('-i', '--int-value', type=int, help='Set an integer value.', default=0)
    parser.add_argument('-f', '--float-value', type=float, help='Set a float value.', default=0.0)
    parser.add_argument('-u', '--num-uses', help='The number of uses. Default is 1', default=1)
    parser.add_argument('-d', '--do-not-delete', action='store_true', help='Sets the doNotDelete property')
    parser.add_argument('-r', '--remove', action='store_true', help='Remove the REI file. The default is to create it')
    parser.add_argument('-C', '--cluster-namespace', action='store_true', help='Use the cluster namespace. By default the management service namespace is used')
    parser.add_argument('-p', '--pod-name', help='A pod name. Fixed as "'+CENTRALD_POD+'" in the management namespace and defaults to "'+CLUSTERD_POD+'" in the cluster namespace or else an agentd pod name is expected')
    parser.add_argument('--kubectl-context', help='Name of the kubectl context. Defaults to '+DEF_KUBECTL_CONTEXT, default=DEF_KUBECTL_CONTEXT)
    parser.add_argument('--log_dirpath', help='Log directory. Defaults to '+DEF_LOG_DIR, default=DEF_LOG_DIR)
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose')
    args = parser.parse_args()
    args.log_dirpath = os.path.expanduser(args.log_dirpath)
    if args.cluster_namespace:
        args.namespace = NS_MANAGED_CLUSTER
        if args.pod_name in [None, CLUSTERD_POD]:
            args.pod_name = CLUSTERD_POD
            args.container = CLUSTERD_CONTAINER
        else:
            args.container = AGENTD_CONTAINER
    else:
        args.namespace = NS_MANAGEMENT_SERVICE
        args.pod_name = CENTRALD_POD
        args.container = CENTRALD_CONTAINER
    if args.verbose:
        print("=" * 78)
        print("       Namespace:", args.namespace)
        print("             Pod:", args.pod_name)
        print("       Container:", args.container)
        print(" Kubectl context:", args.kubectl_context)
        print("   Log directory:", args.log_dirpath)
        print("=" * 78)
        print("        REI Name:", args.rei_name)
        if not args.remove:
            print("   Boolean Value:", args.bool_value)
            print("    String Value:", args.string_value)
            print("   Integer Value:", args.int_value)
            print("     Float Value:", args.float_value)
            print("        Num Uses:", args.num_uses)
            print("   Do Not Delete:", args.do_not_delete)
        else:
            print("          Remove:", args.remove)
    pathlib.Path(args.log_dirpath).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(format='%(asctime)s %(message)s', filename=args.log_dirpath + "/" + os.path.basename(__file__) + ".log", level=logging.INFO)
    logging.info("args: %s", args)
    kch = KubectlHelper(args, context=args.kubectl_context)
    if args.remove:
        kch.rei_clear(args.rei_name, args.pod_name, container_name=args.container, namespace=args.namespace)
    else:
        ret = kch.rei_set(args.rei_name, args.pod_name, container_name=args.container, namespace=args.namespace, bool_value=args.bool_value, string_value=args.string_value, int_value=args.int_value, float_value=args.float_value, num_uses=args.num_uses, do_not_delete=args.do_not_delete)
        print(ret)

if __name__ == '__main__':
    main()
