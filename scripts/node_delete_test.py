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

This tests node deletion behavior.

Prerequisites:
    1. Requires kontroller to be deployed with 'rei' enabled.

Usage:

To run all tests:
PYTHONPATH=~/testing/lib/ python3 ~/testing/scripts/node_delete_test.py -H ad03684c2ec7111e988470ad853240bb-582513320.us-west-2.elb.amazonaws.com \
    --app_cluster_name test.k8s.local --mgmt_cluster_name mgmt.k8s.local --nuvo_cluster_name nuvoauto-2d07d run --confirm

If app cluster is not created, do not pass the --nuvo_cluster_name option. The test will automatically create a kops cluster.

To run specific cases, use "run -c [0-7]"

To view list of testcases, use "view-setup-cases"

"""

import argparse
import copy
import logging
import os
import pathlib
import subprocess
import time

import botocore

from nuvoloso.api.nuvo_management import NuvoManagement
from nuvoloso.dependencies.aws import AWS
from nuvoloso.dependencies.kubectl_helper import KON_NAMESPACE as NS_MANAGEMENT
import nuvoloso.api.obj_helper as obj_helper
from nuvoloso.dependencies.kubectl_helper import KubectlHelper
from nuvoloso.dependencies.install_packages import InstallPackages
from nuvoloso.dependencies.kops_cluster import KopsCluster

DEF_ACCOUNT_NAME = 'Normal Account'
DEF_CENTRALD_CONTAINER = "centrald"
DEF_CENTRALD_POD = "services-0"
DEF_CLUSTERD_CONTAINER = "clusterd"
DEF_CLUSTERD_POD = "clusterd-0"
DEF_CLUSTER_NAME = 'nuvotest.k8s.local'
DEF_SERVICE_PLAN = 'General'
DEF_TENANT_NAME = 'Demo Tenant'

REI_SREQ_BLOCK_IN_ATTACH = 'sreq/block-in-attach'
REI_SREQ_BLOCK_NUVO_CLOSE_DEVICE = 'sreq/block-nuvo-close-device'
REI_SREQ_BLOCK_NUVO_USE_DEVICE = 'sreq/block-nuvo-use-device'
REI_SREQ_FORMAT_TMP_ERROR = 'sreq/format-tmp-error'
REI_VREQ_PLACEMENT_BLOCK_ON_VS_UPDATE = 'vreq/placement-block-on-vs-update'
REI_VREQ_SIZE_BLOCK_ON_START = 'vreq/size-block-on-start'
REI_VREQ_TEARDOWN_FATAL_ERROR = 'vreq/teardown-fatal-error'
REI_VREQ_VSC_BLOCK_ON_START = 'vreq/vsc-block-on-start'

DEFAULT_AMI = '099720109477/ubuntu/images/hvm-ssd/ubuntu-bionic-18.04-amd64-server-20190212.1'

# pylint: disable=too-many-public-methods
class TestNodeDelete():
    """Supports testing of node deletion"""

    def __init__(self, args):
        self.args = args
        self.nuvo_mgmt = NuvoManagement(args)
        args2 = copy.deepcopy(args)
        args2.account_name = args.tenant_account_name
        self.nuvo_mgmt_2 = NuvoManagement(args2)
        self.kubectl_helper = KubectlHelper(args, context=args.app_cluster_name)
        self.mgmt_kubectl_helper = KubectlHelper(args, context=args.mgmt_cluster_name)
        self._aws = None
        self.node_id = self.nuvo_mgmt.get_node_id(self.nuvo_mgmt.get_cluster_id(args.nuvo_cluster_name))
        try:
            self.node = obj_helper.NodeObject()
            self.node.load(self.nuvo_mgmt, obj_id=self.node_id)
            self.cluster = obj_helper.ClusterObject()
            self.cluster.load(self.nuvo_mgmt, obj_id=self.node.clusterId)
        except RuntimeError as err:
            if err.args[1] == 404:
                self.node = None
                self.cluster = None
            else:
                raise

    def aws(self):
        """Returns the cached AWS helper"""
        if self._aws:
            return self._aws
        setattr(self.args, 'region', self.args.region)
        self._aws = AWS(self.args)
        return self._aws

    def account_id(self):
        """Return the id of the account used"""
        return self.nuvo_mgmt.account_id

    def service_plan_id(self):
        """Return the id of the service plan used"""
        return self.nuvo_mgmt.get_serviceplan_id()

    def find_or_create_vol(self, vs_name, ops, **kwargs):
        """Find a named volume series or launch specified VSR to create and initialize it"""
        vs_obj = obj_helper.VolumeSeriesObject()
        try:
            print("Searching for volume", vs_name)
            vs_obj.find(self.nuvo_mgmt, vs_name)
            print("Volume state is", vs_obj.volumeSeriesState)
        except RuntimeError as err:
            print("Volume", vs_name, err)
            kwargs['volume_name'] = vs_name
            vsr_obj = obj_helper.VolumeSeriesRequestObject()
            vsr_obj.assemble(ops, **kwargs)
            print("Volume", vs_name, ": Issuing", ops)
            vsr_obj.run(self.nuvo_mgmt)
            vs_obj.load(self.nuvo_mgmt, obj_id=vsr_obj.volumeSeriesId)
        return vs_obj

    def attempt_to_create_vol(self, vs_name, ops, **kwargs):
        """Search for an active VSR that creates the named volume.
        If not found then launch it with the given arguments.
        If found, wait until it reaches the specified non_terminal_state.
        """
        nts = kwargs.pop('non_terminal_states', None)
        if not nts:
            raise ValueError('expected non_terminal_states kwarg')
        vsr_obj = obj_helper.VolumeSeriesRequestObject()
        found = False
        for obj in vsr_obj.query(self.nuvo_mgmt, {'isTerminated':False}):
            if 'CREATE' in obj.requestedOperations:
                cspec = obj.volumeSeriesCreateSpec
                if cspec["name"] == vs_name:
                    found = True
                    vsr_obj = obj
                    break
        if not found:
            kwargs['volume_name'] = vs_name
            vsr_obj.assemble(ops, **kwargs)
            print("Volume", vs_name, ": Issuing", ops)
            vsr_obj.create(self.nuvo_mgmt)
        vsr_obj.wait(self.nuvo_mgmt, non_terminal_states=nts)
        print("Volume", vs_name, "being created by VSR", vsr_obj.meta_id(), "state:", vsr_obj.volumeSeriesRequestState)
        return vsr_obj

    def find_sr_by_message(self, substr):
        """Find active storage requests by matching messages"""
        sr_obj = obj_helper.StorageRequestObject()
        ret = []
        for obj in sr_obj.query(self.nuvo_mgmt, {'isTerminated':False, 'clusterId':self.cluster.meta_id()}):
            if obj.find_in_messages(substr):
                ret.append(obj)
        return ret

    @classmethod
    def indefinite_sequence(cls, values: list):
        """Return the next of the specified values, repeating the last indefinitely"""
        while len(values) > 1:
            yield values[0]
            values.pop(0)
        while True:
            yield values[0]

    @classmethod
    def spinner(cls):
        """Returns a repetitive sequence representing a twirling stick"""
        values = ['-', '\\', '|', '/']
        i = 0
        while True:
            yield values[i]
            i = (i+1) % len(values)

    @classmethod
    # pylint: disable=too-many-arguments
    def show_progress(cls, msg="", period=0.5, delay=10, bar_char="."):
        """Prints a repetitive sequence at the specified period and returns after a delay"""
        count = int(delay / period)
        pos = 0
        i = 0
        spinner = cls.spinner()
        while i < count:
            print("[{}] {}{}{}".format(next(spinner), bar_char, bar_char*pos, " "*(count-pos+len(msg))), end="\r", flush=True)
            i += 1
            pos = (pos + 1)%count
            time.sleep(period)
        print("[*] {}{}".format(bar_char*(count+1), msg), end="\r", flush=True)

    def setup_block_in_agentd_sr_close_device(self):
        """Setup a VS to block in closing a device on the node. Reentrant"""
        vs_obj = self.find_or_create_vol('AGENTD-BLOCK-SR-CLOSE-DEVICE', ['CREATE', 'BIND', 'PUBLISH', 'MOUNT'],\
            account_id=self.account_id(), vol_size_gib=1, service_plan_id=self.service_plan_id(),\
            cluster_id=self.cluster.meta_id(), node_id=self.node.meta_id())
        print("Setting agentd REI", REI_SREQ_BLOCK_NUVO_CLOSE_DEVICE)
        self.kubectl_helper.rei_set(REI_SREQ_BLOCK_NUVO_CLOSE_DEVICE, self.node.service_pod(), bool_value=True, do_not_delete=True)
        retry_after = self.indefinite_sequence([10, 10, 5])
        while vs_obj.is_provisioned() and vs_obj.volumeSeriesState != "PROVISIONED":
            vsr_obj = obj_helper.VolumeSeriesRequestObject()
            vsr_obj.assemble(['UNMOUNT'], vol_id=vs_obj.meta_id(), node_id=self.node.meta_id())
            try:
                print("Volume", vs_obj.name, "state:", vs_obj.volumeSeriesState, ": Issuing UNMOUNT")
                vsr_obj.run(self.nuvo_mgmt)
            except RuntimeError as err:
                print("Ignoring error:", err)
                time.sleep(next(retry_after))
            try:
                vs_obj.load(self.nuvo_mgmt) # reload state
            except RuntimeError as err:
                print("Ignoring error:", err)
        while vs_obj.volumeSeriesState != "UNBOUND":
            found = False
            for vsr_obj in vs_obj.vol_vsrs(self.nuvo_mgmt, is_terminated=False):
                if vsr_obj.requestedOperations == ['UNBIND']:
                    found = True
                    print("Volume", vs_obj.name, "state:", vs_obj.volumeSeriesState, ": found UNBIND VSR in state", vsr_obj.volumeSeriesRequestState)
                    break
            if not found:
                try:
                    print("Volume", vs_obj.name, "state:", vs_obj.volumeSeriesState, ": Issuing UNBIND VSR")
                    vsr_obj = obj_helper.VolumeSeriesRequestObject()
                    vsr_obj.assemble(['UNBIND'], vol_id=vs_obj.meta_id(), cluster_id=self.cluster.meta_id())
                    vsr_obj.create(self.nuvo_mgmt)
                    vsr_obj.run(self.nuvo_mgmt)
                except RuntimeError as err:
                    print("Ignoring error:", err)
            try:
                time.sleep(5)
                vs_obj.load(self.nuvo_mgmt) # reload state
            except RuntimeError as err:
                print("Ignoring error:", err)
        # need to wait for clusterd to release the storage
        retry_after = self.indefinite_sequence([30, 30, 20, 20, 20, 10])
        waiting_on = vs_obj.extract_storage_ids_from_messages()
        while waiting_on:
            new_waiting_on = []
            for storage_id in waiting_on:
                try:
                    s_obj = obj_helper.StorageObject()
                    s_obj.load(self.nuvo_mgmt, obj_id=storage_id) # or reload
                    print("Storage", storage_id, "deviceState:", s_obj.device_state())
                    if s_obj.device_state() == "OPEN":
                        new_waiting_on.append(storage_id)
                except RuntimeError as err:
                    print("Ignoring error:", err)
                    # assume storage is gone
            waiting_on = new_waiting_on
            if waiting_on:
                print("Waiting for Storage to be released", waiting_on)
                time.sleep(next(retry_after))
        print("Volume", vs_obj.name, "setup complete")

    def setup_block_in_agentd_vsr_start(self):
        """Setup a VS to block on starting in agentd. Reentrant"""
        vs_obj = self.find_or_create_vol('AGENTD-BLOCK-VSR-ON-START', ['CREATE', 'BIND', 'PUBLISH', 'MOUNT', 'ATTACH_FS'],\
            account_id=self.account_id(), vol_size_gib=1, service_plan_id=self.service_plan_id(),\
            cluster_id=self.cluster.meta_id(), node_id=self.node.meta_id(), target_path='/mnt')
        print("Setting agentd REI", REI_VREQ_VSC_BLOCK_ON_START)
        self.kubectl_helper.rei_set(REI_VREQ_VSC_BLOCK_ON_START, self.node.service_pod(), bool_value=True, do_not_delete=True)
        retry_after = self.indefinite_sequence([10, 10, 5])
        count = 0
        found = False
        while not found or count < 2: # got to see this at least twice to ensure that the VSR stays blocked
            for vsr_obj in vs_obj.vol_vsrs(self.nuvo_mgmt, is_terminated=False):
                if vsr_obj.requestedOperations == ['VOL_SNAPSHOT_CREATE']:
                    if vsr_obj.volumeSeriesRequestState == 'PAUSING_IO':
                        found = True
                        count += 1
                        print("Volume", vs_obj.name, "state:", vs_obj.volumeSeriesState, ": found VOL_SNAPSHOT_CREATE VSR in state", vsr_obj.volumeSeriesRequestState, "count(max 2):", count)
                    else:
                        print("Volume", vs_obj.name, "state:", vs_obj.volumeSeriesState, ": found VOL_SNAPSHOT_CREATE VSR in state", vsr_obj.volumeSeriesRequestState)
                    break
            if not found:
                try:
                    print("Volume", vs_obj.name, "state:", vs_obj.volumeSeriesState, ": Issuing VOL_SNAPSHOT_CREATE VSR")
                    vsr_obj = obj_helper.VolumeSeriesRequestObject()
                    vsr_obj.assemble(['VOL_SNAPSHOT_CREATE'], vol_id=vs_obj.meta_id())
                    vsr_obj.create(self.nuvo_mgmt)
                    vsr_obj.wait(self.nuvo_mgmt, non_terminal_states=['PAUSING_IO'])
                    count = 1
                except RuntimeError as err:
                    print("Ignoring error:", err)
            if not found or count < 2:
                time.sleep(next(retry_after))
        print("Volume", vs_obj.name, "setup complete")

    def setup_block_in_agentd_sr_use_device(self):
        """Setup a VS to block in use device on the node. Reentrant"""
        print("Setting agentd REI", REI_SREQ_BLOCK_NUVO_USE_DEVICE)
        self.kubectl_helper.rei_set(REI_SREQ_BLOCK_NUVO_USE_DEVICE, self.node.service_pod(), bool_value=True, do_not_delete=True)
        vs_name = 'AGENTD-BLOCK-SR-USE-DEVICE'
        self.attempt_to_create_vol(vs_name, ['CREATE', 'BIND', 'MOUNT'],\
            account_id=self.account_id(), vol_size_gib=1, service_plan_id=self.service_plan_id(),\
            cluster_id=self.cluster.meta_id(), node_id=self.node.meta_id(), non_terminal_states=['STORAGE_WAIT'])
        retry_after = self.indefinite_sequence([30, 20, 5])
        while True:
            sr_objs = self.find_sr_by_message(REI_SREQ_BLOCK_NUVO_USE_DEVICE)
            if sr_objs:
                for sr_obj in sr_objs:
                    print('Volume', vs_name, 'waiting on SR', sr_obj.meta_id())
                break
            time.sleep(next(retry_after))
        print("Volume", vs_name, "setup complete")

    def setup_block_in_agentd_format(self):
        """Setup a VS to block in format on the node. Reentrant"""
        print("Setting agentd REI", REI_SREQ_FORMAT_TMP_ERROR)
        self.kubectl_helper.rei_set(REI_SREQ_FORMAT_TMP_ERROR, self.node.service_pod(), bool_value=True, do_not_delete=True)
        vs_name = 'AGENTD-BLOCK-SR-FORMAT-DEVICE'
        self.attempt_to_create_vol(vs_name, ['CREATE', 'BIND', 'MOUNT'],\
            account_id=self.account_id(), vol_size_gib=1, service_plan_id=self.service_plan_id(),\
            cluster_id=self.cluster.meta_id(), node_id=self.node.meta_id(), non_terminal_states=['STORAGE_WAIT'])
        retry_after = self.indefinite_sequence([30, 20, 5])
        while True:
            sr_objs = self.find_sr_by_message(REI_SREQ_FORMAT_TMP_ERROR)
            if sr_objs:
                for sr_obj in sr_objs:
                    print('Volume', vs_name, 'waiting on SR', sr_obj.meta_id())
                break
            time.sleep(next(retry_after))
        print("Volume", vs_name, "setup complete")

    def setup_clusterd_block_vsr_in_transition(self):
        """Setup a VS to block during mount in clusterd ready to transition to agentd when the block is removed. Reentrant"""
        vs_obj = self.find_or_create_vol('CLUSTERD-BLOCK-VSR-IN-TRANSITION', ['CREATE', 'BIND'],\
            account_id=self.account_id(), vol_size_gib=1, service_plan_id=self.service_plan_id(), cluster_id=self.cluster.meta_id())
        print("Setting clusterd REI", REI_VREQ_PLACEMENT_BLOCK_ON_VS_UPDATE)
        self.kubectl_helper.rei_set(REI_VREQ_PLACEMENT_BLOCK_ON_VS_UPDATE, DEF_CLUSTERD_POD, container_name=DEF_CLUSTERD_CONTAINER, string_value=vs_obj.meta_id(), do_not_delete=True)
        vsr_found = False
        vsr_obj = obj_helper.VolumeSeriesRequestObject()
        for obj in vsr_obj.query(self.nuvo_mgmt, {'isTerminated':False, 'volumeSeriesId': vs_obj.meta_id()}):
            if obj.volumeSeriesRequestState == 'PLACEMENT':
                vsr_found = True
                vsr_obj = obj
                break
        retry_after = self.indefinite_sequence([30, 20, 10, 5])
        is_blocked = False
        while not is_blocked:
            try:
                if not vsr_found:
                    print("Volume", vs_obj.name, ": Issuing ['MOUNT']")
                    vsr_obj.assemble(['MOUNT'], vol_id=vs_obj.meta_id(), account_id=self.account_id(), node_id=self.node.meta_id())
                    vsr_obj.create(self.nuvo_mgmt)
                    vsr_obj.wait(self.nuvo_mgmt, non_terminal_states=['PLACEMENT'])
                if not vsr_obj.is_terminated():
                    is_blocked = vsr_obj.find_in_messages(REI_VREQ_PLACEMENT_BLOCK_ON_VS_UPDATE)
                else:
                    vsr_found = False
                print('Volume', vs_obj.name, 'VSR', vsr_obj.meta_id(), 'state:', vsr_obj.volumeSeriesRequestState, "blocked:", is_blocked)
            except RuntimeError as err:
                print("Ignoring error:", err)
            if not is_blocked:
                time.sleep(next(retry_after))
        print("Volume", vs_obj.name, "setup complete")

    def setup_block_in_centrald_attach(self):
        """Setup a VS to block during mount in centrald doing a storage ATTACH. Reentrant"""
        print("Setting centrald REI", REI_SREQ_BLOCK_IN_ATTACH)
        self.mgmt_kubectl_helper.rei_set(REI_SREQ_BLOCK_IN_ATTACH, DEF_CENTRALD_POD, container_name=DEF_CENTRALD_CONTAINER, namespace=NS_MANAGEMENT, bool_value=True, do_not_delete=True)
        vs_name = 'CENTRALD-BLOCK-IN-ATTACH'
        self.attempt_to_create_vol(vs_name, ['CREATE', 'BIND', 'MOUNT'],\
            account_id=self.account_id(), vol_size_gib=1, service_plan_id=self.service_plan_id(),\
            cluster_id=self.cluster.meta_id(), node_id=self.node.meta_id(), non_terminal_states=['STORAGE_WAIT'])
        retry_after = self.indefinite_sequence([30, 20, 5])
        while True:
            sr_objs = self.find_sr_by_message(REI_SREQ_BLOCK_IN_ATTACH)
            if sr_objs:
                for sr_obj in sr_objs:
                    print('Volume', vs_name, 'waiting on SR', sr_obj.meta_id())
                break
            time.sleep(next(retry_after))
        print("Volume", vs_name, "setup complete")

    def setup_block_in_clusterd_size(self):
        """Setup a VS to block during mount in clusterd. Reentrant"""
        print("Setting clusterd REI", REI_VREQ_SIZE_BLOCK_ON_START)
        self.kubectl_helper.rei_set(REI_VREQ_SIZE_BLOCK_ON_START, DEF_CLUSTERD_POD, container_name=DEF_CLUSTERD_CONTAINER, bool_value=True, do_not_delete=True)
        vs_name = 'CLUSTERD-BLOCK-VSR-SIZING'
        self.attempt_to_create_vol(vs_name, ['CREATE', 'BIND', 'MOUNT'],\
            account_id=self.account_id(), vol_size_gib=1, service_plan_id=self.service_plan_id(),\
            cluster_id=self.cluster.meta_id(), node_id=self.node.meta_id(), non_terminal_states=['SIZING'])
        print("Volume", vs_name, "setup complete")

    def setup_node_delete_failure_in_centrald(self):
        """Setup failure of NODE_DELETE in centrald. Reentrant"""
        print("Setting centrald REI", REI_VREQ_TEARDOWN_FATAL_ERROR)
        self.mgmt_kubectl_helper.rei_set(REI_VREQ_TEARDOWN_FATAL_ERROR, DEF_CENTRALD_POD, container_name=DEF_CENTRALD_CONTAINER, namespace=NS_MANAGEMENT, bool_value=True, do_not_delete=True)

    @classmethod
    def setup_cases(cls):
        """List of setup cases in order"""
        return [
            "block_in_agentd_sr_close_device",
            "block_in_agentd_vsr_start",
            "clusterd_block_vsr_in_transition",
            "block_in_agentd_sr_use_device",
            "block_in_agentd_format",
            "block_in_centrald_attach",
            "block_in_clusterd_size",
            "node_delete_failure_in_centrald"
        ]

    def do_setup(self, run_cases=None):
        """Perform pre-failure setup"""
        cases = self.setup_cases()
        if not run_cases:
            run_cases = range(len(cases))
        else:
            run_cases.sort()
        for i in run_cases:
            print("Setup case {}: {}".format(i, cases[i]))
            method = "setup_" + cases[i]
            getattr(self, method)()
            print("="*78)

    def remove_agentd_rei(self):
        """Remove REI from the node"""
        if not self.node or self.node.state == 'TEAR_DOWN':
            print("Skipping removal of agentd REI as node missing or in TEAR_DOWN")
            return
        for rei in [REI_SREQ_BLOCK_NUVO_CLOSE_DEVICE, REI_SREQ_FORMAT_TMP_ERROR, REI_SREQ_BLOCK_NUVO_USE_DEVICE, REI_VREQ_VSC_BLOCK_ON_START]:
            try:
                print("Clearing agentd REI", rei)
                self.kubectl_helper.rei_clear(rei, self.node.service_pod())
            except subprocess.CalledProcessError as err:
                print(err)

    def remove_clusterd_rei(self):
        """Remove REI from clusterd"""
        for rei in [REI_VREQ_PLACEMENT_BLOCK_ON_VS_UPDATE, REI_VREQ_SIZE_BLOCK_ON_START]:
            try:
                print("Clearing clusterd REI", rei)
                self.kubectl_helper.rei_clear(rei, DEF_CLUSTERD_POD, container_name=DEF_CLUSTERD_CONTAINER)
            except subprocess.CalledProcessError as err:
                print(err)

    def remove_centrald_rei(self, clear_reis=None):
        """Remove REI from centrald"""
        if not clear_reis:
            clear_reis = [REI_VREQ_TEARDOWN_FATAL_ERROR, REI_SREQ_BLOCK_IN_ATTACH]
        for rei in clear_reis:
            try:
                print("Clearing centrald REI", rei)
                self.mgmt_kubectl_helper.rei_clear(rei, DEF_CENTRALD_POD, container_name=DEF_CENTRALD_CONTAINER, namespace=NS_MANAGEMENT)
            except subprocess.CalledProcessError as err:
                print(err)

    def check_can_terminate_node(self, verbose=False):
        """Checks if the invoker can terminate the node"""
        try:
            self.aws().terminate_instances([self.node.aws_instance()], dry_run=True)
        except botocore.exceptions.ClientError as err:
            if err.response['Error']['Code'] == 'DryRunOperation':
                pass
            elif err.response['Error']['Code'] == 'UnauthorizedOperation':
                print("Not authorized to delete node")
                return False
            else:
                raise
        if verbose:
            print("Invoker can delete node")
        return True

    def trigger_failure(self):
        """Initiate node failure and perform post-failure actions. Reentrant"""
        node_id = self.node_id
        if self.node:
            # the node may have been deleted already
            instance_id = self.node.aws_instance()
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
                    if self.node.service_state() in ['STOPPED', 'UNKNOWN']:
                        break
                    self.node.load(self.nuvo_mgmt) # could throw if the node is deleted
                except RuntimeError:
                    print("Node object error")
                    break
                time.sleep(next(retry_after))
        self.remove_clusterd_rei()
        self.remove_centrald_rei(clear_reis=[REI_SREQ_BLOCK_IN_ATTACH])
        print("="*78)
        print("Monitoring NODE_DELETE account:", self.args.tenant_account_name)
        node_delete_done = False
        last_state = {}
        removed_centrald_rei = False
        query = {'requestedOperations':['NODE_DELETE'], 'nodeId':node_id}
        in_spinner = False
        while not node_delete_done:
            vsr_obj = obj_helper.VolumeSeriesRequestObject()
            ret = vsr_obj.query(self.nuvo_mgmt_2, query)
            for obj in ret:
                if obj.meta_id() not in last_state or last_state[obj.meta_id()] != obj.volumeSeriesRequestState:
                    if in_spinner:
                        print("\n")
                        in_spinner = False
                    print("NODE_DELETE", obj.meta_id(), "state:", obj.volumeSeriesRequestState)
                    last_state[obj.meta_id()] = obj.volumeSeriesRequestState
                if obj.is_terminated():
                    if obj.volumeSeriesRequestState == 'FAILED':
                        if not removed_centrald_rei and len(ret) == 1:
                            self.remove_centrald_rei()
                            removed_centrald_rei = True
                    else:
                        node_delete_done = True
            if not node_delete_done:
                in_spinner = True
                self.show_progress(msg="checking")

    def cleanup_volumes(self):
        """Best effort cleanup"""
        for vs_name in ['AGENTD-BLOCK-SR-CLOSE-DEVICE', 'AGENTD-BLOCK-VSR-ON-START', 'AGENTD-BLOCK-SR-USE-DEVICE', 'AGENTD-BLOCK-SR-FORMAT-DEVICE', 'CLUSTERD-BLOCK-VSR-IN-TRANSITION', 'CLUSTERD-BLOCK-VSR-SIZING', 'CENTRALD-BLOCK-IN-ATTACH']:
            vs_obj = obj_helper.VolumeSeriesObject()
            try:
                vs_obj.find(self.nuvo_mgmt, vs_name)
                vsr_obj = obj_helper.VolumeSeriesRequestObject()
                if vs_obj.is_in_use():
                    ops = ['UNMOUNT', 'DELETE']
                    args = {'vol_id':vs_obj.meta_id(), 'node_id':vs_obj.mounted_node()}
                    fs_path = vs_obj.target_path()
                    if fs_path:
                        ops.append('DETACH_FS')
                        args["target_path"] = fs_path
                    vsr_obj.assemble(ops, **args)
                else:
                    vsr_obj.assemble(['DELETE'], vol_id=vs_obj.meta_id())
                vsr_obj.create(self.nuvo_mgmt)
                print("Volume", vs_name, ": created", vsr_obj.meta_id(), vsr_obj.requestedOperations, "to cleanup")
            except RuntimeError as err:
                print("Volume", vs_obj, err)

class appClusterCreate:
    def __init__(self, args):
        self.args = args
        self.nuvo_mgmt = NuvoManagement(args)

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
            self.nuvo_mgmt.switch_accounts(self.args.tenant_account_name)
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

def main():
    """main"""
    parser = argparse.ArgumentParser(description="Test node deletion")
    parser.add_argument('-H', '--nuvo_kontroller_hostname', help='Hostname of Nuvo Kontroller management service', required=True)
    parser.add_argument('-A', '--account_name', help='Nuvoloso subordinate account name. Defaults to '+DEF_ACCOUNT_NAME, default=DEF_ACCOUNT_NAME)
    parser.add_argument('-T', '--tenant-account_name', help='Nuvoloso tenant account name. Defaults to '+DEF_TENANT_NAME, default=DEF_TENANT_NAME)
    parser.add_argument('--serviceplan_name', help='Name of service plan. Defaults to '+DEF_SERVICE_PLAN, default=DEF_SERVICE_PLAN)
    parser.add_argument('--app_cluster_name', help='Name of the application cluster.', required=True)
    parser.add_argument('--mgmt_cluster_name', help='Name of the cluster where kontroller is deployed', required=True)
    parser.add_argument('--nuvo_cluster_name', help='Cluster obj name created in kontroller')
    parser.add_argument('--aws_profile_name', help='Name of the AWS profile in the credential file')
    parser.add_argument('--region', help='Name of the AWS region', default='us-west-2')
    parser.add_argument('-L', '--log_dirpath', help='Log directory. Defaults to ~/node-test-log', default='~/node-test-log')
    parser.add_argument('--do_cleanup', action='store_true', help='Pass flag to cleanup cluster. Default: False')
    parser.add_argument('--kops_state_store', help='state store for both clusters')
    parser.add_argument('--aws_access_key', help='aws AccessKey')
    parser.add_argument('--aws_secret_access_key', help='aws SecretAccessKey')
    parser.add_argument('--kubernetes_version', help='version of kubernetes to deploy', default=None)
    parser.add_argument('--image', help='AMI Image for all instances', default=DEFAULT_AMI)
    parser.add_argument('--k8s_master_zone', help='aws zone for master node', default='us-west-2c')
    parser.add_argument('--k8s_nodes_zone', help='aws zone for other nodes ', default='us-west-2c')
    parser.add_argument('--nodes', help='Number of nodes in the cluster [default=3]', type=int, default=3, choices=range(1, 101))
    parser.add_argument('--node_size', help='ec2 instance type for other nodes ', default='m5d.large')
    parser.add_argument('--master_size', help='ec2 instance type for master node ', default='m5d.large')
    parser.add_argument('--node_volume_size', help='volume size for slave nodes of k8s cluster', type=int, default=10)
    parser.add_argument('--master_volume_size', help='volume size for master node of k8s cluster', type=int, default=20)

    subparsers = parser.add_subparsers(title='commands', dest='command_name')

    run_parser = subparsers.add_parser('run', help='Perform the entire test')
    run_parser.add_argument('--confirm', help='Confirm that the operations should be performed', action='store_true', required=True)
    rpg = run_parser.add_mutually_exclusive_group()
    rpg.add_argument('--skip-setup', help='Skip setup steps', action='store_true')
    rpg.add_argument('-c', '--cases', nargs='+', type=int, help='List of setup case numbers')

    subparsers.add_parser('view-setup-cases', help='View the setup cases')

    setup_parser = subparsers.add_parser('setup', help='Perform the setup steps only')
    setup_parser.add_argument('-c', '--cases', nargs='+', type=int, help='List of setup case numbers')

    delete_parser = subparsers.add_parser('delete', help='Terminate the node - see detailed help',\
        usage='delete {flags}\n\nDelete the node and perform post-deletion steps')
    delete_group = delete_parser.add_mutually_exclusive_group(required=True)
    delete_group.add_argument('--confirm', help='Confirm that the operations should be performed', action='store_true')
    delete_group.add_argument('--verify', help='Verify that the invoker can delete the node', action='store_true')

    cleanup_parser = subparsers.add_parser('cleanup', help='Perform cleanup actions - see detailed help', usage='cleanup [flags]\n\nPerform cleanup actions. If no flags specified all actions performed')
    cleanup_parser.add_argument('--agentd-rei', help='Remove REIs from agentd on the targeted node', action='store_true')
    cleanup_parser.add_argument('--centrald-rei', help='Remove REIs from centrald', action='store_true')
    cleanup_parser.add_argument('--clusterd-rei', help='Remove REIs from clusterd', action='store_true')
    cleanup_parser.add_argument('--volumes', help='Cleanup volumes on a best effort basis', action='store_true')

    args = parser.parse_args()
    args.kops_cluster_name = args.app_cluster_name

    if not args.nuvo_cluster_name:
        assert args.kops_state_store, "Required argument: kops_state_store"
        assert args.aws_access_key, "Required argument: AWS access key"
        assert args.aws_secret_access_key, "Required argument: AWS secret access key"

    if args.command_name == 'view-setup-cases':
        cases = TestNodeDelete.setup_cases()
        for case in enumerate(cases):
            print("{}: {}".format(case[0], case[1]))
        return

    args.log_dirpath = os.path.expanduser(args.log_dirpath)
    print("   Management service:", args.nuvo_kontroller_hostname)
    print("        Log directory:", args.log_dirpath)
    print("          AWS profile:", args.aws_profile_name)
    print("           AWS region:", args.region)
    print("  Subordinate account:", args.account_name)
    print("       Tenant account:", args.tenant_account_name)
    print("="*78)
    pathlib.Path(args.log_dirpath).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(format='%(asctime)s %(filename)s:%(lineno)d %(message)s', filename=args.log_dirpath + "/" + os.path.basename(__file__) + ".log", level=logging.INFO)
    logging.info("args: %s", args)

    if not args.nuvo_cluster_name:
        logging.info("Creating Application cluster")
        cluster_create = appClusterCreate(args)
        cluster_create.install_dependencies()
        args.nuvo_cluster_name = cluster_create.create_application_cluster()

    ndt = TestNodeDelete(args)
    if ndt.node:
        print("     Node object name:", ndt.node.name)
        print("           Node State:", ndt.node.service_cooked_state())
        print("  Node local hostname:", ndt.node.local_hostname())
        print(" Node public hostname:", ndt.node.public_hostname())
        print("         AWS instance:", ndt.node.aws_instance())
        print("             Pod name:", ndt.node.service_pod())
        print("           Cluster ID:", ndt.node.clusterId)
        print("         Cluster Name:", ndt.cluster.name)
        print("        Cluster State:", ndt.cluster.service_cooked_state())
    else:
        print("Node object not found: assumed deleted")
    print("="*78)

    if args.command_name == 'run':
        if ndt.node:
            if not ndt.check_can_terminate_node():
                return
            if not args.skip_setup and ndt.node.service_cooked_state() == 'MANAGED/READY':
                ndt.do_setup(run_cases=args.cases)
            elif not args.skip_setup:
                print("Skipping setup steps as node is not MANAGED/READY")
        ndt.trigger_failure()
        ndt.remove_agentd_rei()
        ndt.remove_clusterd_rei()
        ndt.remove_centrald_rei()
        ndt.cleanup_volumes()
    elif args.command_name == 'setup':
        if ndt.node and ndt.node.service_cooked_state() == 'MANAGED/READY':
            ndt.do_setup(run_cases=args.cases)
        else:
            print("Skipping setup steps as node missing or not MANAGED/READY")
    elif args.command_name == 'delete':
        if args.verify:
            ndt.check_can_terminate_node(verbose=True)
        elif args.confirm:
            ndt.trigger_failure()
    elif args.command_name == 'cleanup':
        i = 0
        if args.agentd_rei:
            ndt.remove_agentd_rei()
            i += 1
        if args.clusterd_rei:
            ndt.remove_clusterd_rei()
            i += 1
        if args.centrald_rei:
            ndt.remove_centrald_rei()
            i += 1
        if args.volumes:
            ndt.cleanup_volumes()
            i += 1
        if i == 0:
            ndt.remove_agentd_rei()
            ndt.remove_clusterd_rei()
            ndt.remove_centrald_rei()
            ndt.cleanup_volumes()

    if args.do_cleanup:
        logging.info("Test succeeded. Deleting kops cluster now")
        KopsCluster.kops_delete_cluster(kops_cluster_name=args.app_cluster_name,
                                        kops_state_store=args.kops_state_store)
        domain_id = ndt.nuvo_mgmt.get_csp_domain_id_from_cluster(args.nuvo_cluster_name)
        ndt.nuvo_mgmt.wait_for_cluster_state(args.nuvo_cluster_name, "TIMED_OUT", domain_id=domain_id)
        ndt.kubectl_helper.cleanup_cluster(args.nuvo_cluster_name, \
            ndt.nuvo_mgmt.get_csp_domain(domain_id)[0]['name'] , args.tenant_account_name)
    else:
        logging.info("Test succeeded. Skipping kops delete cluster since do_cleanup is False")

if __name__ == '__main__':
    main()
