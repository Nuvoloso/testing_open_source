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

VSR object helper tests
"""

import json
import os.path
from unittest.mock import patch

from nuvoloso.api import obj_helper, nuvo_management

def test_vsr_object():
    """Test VolumeSeriesRequestObject methods"""
    data_dir = os.path.dirname(os.path.abspath(__file__))
    with open(data_dir+"/data/vsr.json") as data_fp:
        obj_json = json.load(data_fp)
        obj = obj_helper.VolumeSeriesRequestObject(obj_json)
    assert obj is not None, "vsr is not None"
    assert obj.raw() is not None, "vsr has data"
    assert obj.raw() == obj_json, "vsr raw == json"
    assert obj.url_type() == nuvo_management.VOL_SERIES_REQUESTS
    assert obj.meta_id() == 'e0f83683-173e-40aa-a616-29cd845d5a53', "obj.meta_id() ok"
    assert obj.meta_version() == 16, "obj.meta_version() ok"
    assert obj.clusterId == "1bbed242-1ce6-45e6-8172-aa1e370ccb9f", "obj.clusterId ok"
    assert obj.nodeId == "5ec2be07-b88f-4c19-b53c-465156df1f08", "obj.nodeId ok"
    assert obj.volumeSeriesRequestState == "SUCCEEDED", "obj.volumeSeriesRequestState ok"
    assert obj.volumeSeriesCreateSpec["name"] == 'V-1', "obj.volumeSeriesCreateSpec ok"
    assert obj.volumeSeriesId == "addce014-414d-43b6-ad53-d9b4bb918422", "obj.volumeSeriesId ok"
    assert obj.find_in_messages("vreq/placement-block-on-vs-update")

    try:
        print(obj.foo)
    except KeyError:
        pass
    else:
        assert False, "obj.foo should throw"
    obj = obj_helper.VolumeSeriesRequestObject()
    assert obj is not None, "empty cluster is not None"


def test_vsr_create_sequence_with_specified_name():
    """Test VolumeSeriesRequestObject CREATE-BIND-PUBLISH-MOUNT-ATTACH_FS sequence"""
    obj = obj_helper.VolumeSeriesRequestObject(None)
    assert obj is not None, "vsr can be uninitialized"
    try:
        obj.assemble(['CREATE', 'BIND', 'PUBLISH', 'MOUNT', 'ATTACH_FS'],\
            volume_name='V-1', account_id='A-1', vol_size_gib=1, service_plan_id='SP-1',\
            cluster_id='C-1', node_id='N-1', target_path='/mnt', vol_tags=['vs-tag1'])
    except ValueError as err:
        assert False, "assemble should not throw: {}".format(err.args)
    else:
        assert obj.requestedOperations == ['CREATE', 'BIND', 'PUBLISH', 'MOUNT', 'ATTACH_FS'], "vsr requestedOperations set"
        assert obj.completeByTime is not None, "vsr completeByTime set"
        assert obj.volumeSeriesCreateSpec is not None, "vsr volumeSeriesCreateSpec set"
        cspec = obj.volumeSeriesCreateSpec
        assert cspec["accountId"] == 'A-1', "accountId set"
        assert cspec["servicePlanId"] == 'SP-1', "servicePlanId set"
        assert cspec["name"] == 'V-1', "name set"
        assert obj.volumeSeriesCreateSpec["name"] == 'V-1', "name retrievable"
        assert cspec["sizeBytes"] == 1 << 30, "sizeBytes set"
        assert cspec["tags"] == ['vs-tag1'], "creation tags set"
        assert obj.clusterId == 'C-1', "vsr clusterId set"
        assert obj.nodeId == 'N-1', "vsr nodeId set"
        assert obj.targetPath == '/mnt', "vsr targetPath set"


def test_vsr_create_sequence_with_auto_naming():
    """Test VolumeSeriesRequestObject CREATE with auto-naming"""
    obj = obj_helper.VolumeSeriesRequestObject(None)
    assert obj is not None, "vsr can be uninitialized"
    try:
        obj.assemble(['CREATE'], account_id='A-1', vol_size_gib=2, service_plan_id='SP-1')
    except ValueError as err:
        assert False, "assemble should not throw: {}".format(err.args)
    else:
        assert obj.volumeSeriesCreateSpec is not None, "vsr volumeSeriesCreateSpec set"
        cspec = obj.volumeSeriesCreateSpec
        assert cspec["accountId"] == 'A-1', "accountId set"
        assert cspec["servicePlanId"] == 'SP-1', "servicePlanId set"
        assert cspec["name"].startswith("nuvoauto-vol-"), "name generated"
        assert cspec["sizeBytes"] == 2 << 30, "sizeBytes set"


def test_vsr_unmount():
    """Test VolumeSeriesRequestObject UNMOUNT"""
    obj = obj_helper.VolumeSeriesRequestObject(None)
    assert obj is not None, "vsr can be uninitialized"
    try:
        obj.assemble(['DETACH_FS', 'UNMOUNT'], vol_id='VS-1', node_id='N-1', target_path='/mnt')
    except ValueError as err:
        assert False, "assemble should not throw: {}".format(err.args)
    else:
        assert obj.requestedOperations == ['DETACH_FS', 'UNMOUNT'], "vsr requestedOperations set"
        assert obj.volumeSeriesId == 'VS-1', "vsr volumeSeriesId set"
        assert obj.nodeId == 'N-1', "vsr nodeId set"
        assert obj.targetPath == '/mnt', "vsr targetPath set"


def test_vsr_unbind():
    """Test VolumeSeriesRequestObject UNBIND"""
    obj = obj_helper.VolumeSeriesRequestObject(None)
    assert obj is not None, "vsr can be uninitialized"
    try:
        obj.assemble(['UNBIND'], vol_id='VS-1')
    except ValueError as err:
        assert False, "assemble should not throw: {}".format(err.args)
    else:
        assert obj.requestedOperations == ['UNBIND'], "vsr requestedOperations set"
        assert obj.volumeSeriesId == 'VS-1', "vsr volumeSeriesId set"


def test_vsr_vol_snapshot_create():
    """Test VolumeSeriesRequestObject VOL_SNAPSHOT_CREATE"""
    obj = obj_helper.VolumeSeriesRequestObject(None)
    assert obj is not None, "vsr can be uninitialized"
    try:
        obj.assemble(['VOL_SNAPSHOT_CREATE'], vol_id='VS-1')
    except ValueError as err:
        assert False, "assemble should not throw: {}".format(err.args)
    else:
        assert obj.requestedOperations == ['VOL_SNAPSHOT_CREATE'], "vsr requestedOperations set"
        assert obj.volumeSeriesId == 'VS-1', "vsr volumeSeriesId set"


def test_vsr_cg_snapshot_create():
    """Test VolumeSeriesRequestObject CG_SNAPSHOT_CREATE"""
    obj = obj_helper.VolumeSeriesRequestObject(None)
    assert obj is not None, "vsr can be uninitialized"
    try:
        obj.assemble(['CG_SNAPSHOT_CREATE'], cg_id='CG-1', cluster_id='CL-1')
    except ValueError as err:
        assert False, "assemble should not throw: {}".format(err.args)
    else:
        assert obj.requestedOperations == ['CG_SNAPSHOT_CREATE'], "vsr requestedOperations set"
        assert obj.consistencyGroupId == 'CG-1', "vsr consistencyGroupId set"
        assert obj.clusterId == 'CL-1', "vsr clusterId set"


@patch('nuvoloso.api.nuvo_management.NuvoManagement', url_base='/api/v1/', spec=nuvo_management.NuvoManagement)
def test_vsr_create(mock_nuvo_mgmt):
    """Test invocation of create"""
    obj = obj_helper.VolumeSeriesRequestObject({'key':'value'})
    assert obj is not None, "vsr is initialized"
    mock_nuvo_mgmt.api_request.return_value = {'something':'else'}
    obj.create(mock_nuvo_mgmt)
    assert mock_nuvo_mgmt.api_request.called
    assert mock_nuvo_mgmt.api_request.call_args[0] == ('POST', '/api/v1/volume-series-requests')
    assert mock_nuvo_mgmt.api_request.call_args[1] == {'json_data': json.dumps({'key':'value'})}
    assert obj.something == 'else', "obj updated"


@patch('nuvoloso.api.nuvo_management.NuvoManagement', url_base='/api/v1/', spec=nuvo_management.NuvoManagement)
def test_vsr_wait_success(mock_nuvo_mgmt):
    """Test invocation of wait"""
    obj = obj_helper.VolumeSeriesRequestObject({'meta':{'id':'vsr-1'}})
    assert obj is not None, "vsr is initialized"
    assert obj.meta_id() == 'vsr-1'
    mock_nuvo_mgmt.api_request.side_effect = [ \
        {'meta':{'id':'vsr-1'}, 'volumeSeriesRequestState':'ACTIVE'},\
        {'meta':{'id':'vsr-1'}, 'volumeSeriesRequestState':'SUCCEEDED'}\
    ]
    obj.wait(mock_nuvo_mgmt, poll_interval_secs=0)
    assert mock_nuvo_mgmt.api_request.called
    assert mock_nuvo_mgmt.api_request.call_count == 2
    assert mock_nuvo_mgmt.api_request.call_args[0] == ('GET', '/api/v1/volume-series-requests/vsr-1')
    assert obj.volumeSeriesRequestState == 'SUCCEEDED', "obj updated"


@patch('nuvoloso.api.nuvo_management.NuvoManagement', url_base='/api/v1/', spec=nuvo_management.NuvoManagement)
def test_vsr_wait_failure(mock_nuvo_mgmt):
    """Test invocation of wait"""
    obj = obj_helper.VolumeSeriesRequestObject({'meta':{'id':'vsr-1'}})
    assert obj is not None, "vsr is initialized"
    assert obj.meta_id() == 'vsr-1'
    mock_nuvo_mgmt.api_request.side_effect = [ \
        {'meta':{'id':'vsr-1'}, 'volumeSeriesRequestState':'ACTIVE'},\
        {'meta':{'id':'vsr-1'}, 'volumeSeriesRequestState':'FAILED'}\
    ]
    obj.wait(mock_nuvo_mgmt, poll_interval_secs=0)
    assert mock_nuvo_mgmt.api_request.called
    assert mock_nuvo_mgmt.api_request.call_count == 2
    assert mock_nuvo_mgmt.api_request.call_args[0] == ('GET', '/api/v1/volume-series-requests/vsr-1')
    assert obj.volumeSeriesRequestState == 'FAILED', "obj updated"


@patch('nuvoloso.api.nuvo_management.NuvoManagement', url_base='/api/v1/', spec=nuvo_management.NuvoManagement)
def test_vsr_wait_canceled(mock_nuvo_mgmt):
    """Test invocation of wait"""
    obj = obj_helper.VolumeSeriesRequestObject({'meta':{'id':'vsr-1'}})
    assert obj is not None, "vsr is initialized"
    assert obj.meta_id() == 'vsr-1'
    mock_nuvo_mgmt.api_request.side_effect = [ \
        {'meta':{'id':'vsr-1'}, 'volumeSeriesRequestState':'ACTIVE'},\
        {'meta':{'id':'vsr-1'}, 'volumeSeriesRequestState':'CANCELED'}\
    ]
    obj.wait(mock_nuvo_mgmt, poll_interval_secs=0)
    assert mock_nuvo_mgmt.api_request.called
    assert mock_nuvo_mgmt.api_request.call_count == 2
    assert mock_nuvo_mgmt.api_request.call_args[0] == ('GET', '/api/v1/volume-series-requests/vsr-1')
    assert obj.volumeSeriesRequestState == 'CANCELED', "obj updated"


@patch('nuvoloso.api.nuvo_management.NuvoManagement', url_base='/api/v1/', spec=nuvo_management.NuvoManagement)
def test_vsr_wait_non_terminal(mock_nuvo_mgmt):
    """Test invocation of wait"""
    obj = obj_helper.VolumeSeriesRequestObject({'meta':{'id':'vsr-1'}})
    assert obj is not None, "vsr is initialized"
    assert obj.meta_id() == 'vsr-1'
    mock_nuvo_mgmt.api_request.side_effect = [ \
        {'meta':{'id':'vsr-1'}, 'volumeSeriesRequestState':'ACTIVE'},\
        {'meta':{'id':'vsr-1'}, 'volumeSeriesRequestState':'FOO_STATE'}\
    ]
    obj.wait(mock_nuvo_mgmt, non_terminal_states=['BAR_STATE', 'FOO_STATE'], poll_interval_secs=0)
    assert mock_nuvo_mgmt.api_request.called
    assert mock_nuvo_mgmt.api_request.call_count == 2
    assert mock_nuvo_mgmt.api_request.call_args[0] == ('GET', '/api/v1/volume-series-requests/vsr-1')
    assert obj.volumeSeriesRequestState == 'FOO_STATE', "obj updated"


@patch('nuvoloso.api.nuvo_management.NuvoManagement', url_base='/api/v1/', spec=nuvo_management.NuvoManagement)
def test_vsr_wait_timeout(mock_nuvo_mgmt):
    """Test invocation of wait"""
    obj = obj_helper.VolumeSeriesRequestObject({'meta':{'id':'vsr-1'}})
    assert obj is not None, "vsr is initialized"
    assert obj.meta_id() == 'vsr-1'
    mock_nuvo_mgmt.api_request.side_effect = [ \
        {'meta':{'id':'vsr-1'}, 'volumeSeriesRequestState':'ACTIVE'},\
        {'meta':{'id':'vsr-1'}, 'volumeSeriesRequestState':'STATE1'}\
    ]
    obj.wait(mock_nuvo_mgmt, poll_interval_secs=1, timeout_after_secs=-1)
    assert not mock_nuvo_mgmt.api_request.called
    assert mock_nuvo_mgmt.api_request.call_count == 0


@patch('nuvoloso.api.nuvo_management.NuvoManagement', url_base='/api/v1/', spec=nuvo_management.NuvoManagement)
def test_vsr_run(mock_nuvo_mgmt):
    """Test invocation of run (create/wait)"""
    obj = obj_helper.VolumeSeriesRequestObject({'meta':{'id':'vsr-1'}})
    assert obj is not None, "vsr is initialized"
    assert obj.meta_id() == 'vsr-1'
    mock_nuvo_mgmt.api_request.side_effect = [ \
        {'meta':{'id':'vsr-1'}, 'volumeSeriesRequestState':'ACTIVE'},\
        {'meta':{'id':'vsr-1'}, 'volumeSeriesRequestState':'STATE1'}\
    ]
    obj.run(mock_nuvo_mgmt, poll_interval_secs=1, timeout_after_secs=-1)
    assert mock_nuvo_mgmt.api_request.called
    assert mock_nuvo_mgmt.api_request.call_count == 1
