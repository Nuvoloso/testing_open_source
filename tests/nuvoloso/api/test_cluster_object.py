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

Cluster object helper tests
"""

import json
import os.path
from nuvoloso.api import obj_helper, nuvo_management

def test_cluster_object():
    """Test ClusterObject methods"""
    data_dir = os.path.dirname(os.path.abspath(__file__))
    with open(data_dir+"/data/cluster.json") as data_fp:
        obj_json = json.load(data_fp)
        obj = obj_helper.ClusterObject(obj_json)
    assert obj is not None, "cluster is not None"
    assert obj.raw() is not None, "cluster has data"
    assert obj.raw() == obj_json, "cluster raw == json"
    assert obj.url_type() == nuvo_management.CLUSTERS
    assert obj.meta_id() == "1bbed242-1ce6-45e6-8172-aa1e370ccb9f", "obj.meta_id() ok"
    assert obj.meta_version() == 3817, "obj.meta_version() ok"
    assert obj.service_type() == "clusterd", "cluster service type ok"
    assert obj.service_pod() == "clusterd-0", "cluster service pod ok"
    assert obj.service_state() == "READY", "cluster service state ok"
    assert obj.service_cooked_state() == "MANAGED/READY", "cluster service state ok"
    assert obj.service_messages() is not None, "cluster service messages ok"
    assert isinstance(obj.service_messages(), list), "cluster service messages is a list"
    assert obj.name == "MyCluster", "obj.name ok"
    assert obj.state == "MANAGED", "obj.state ok"
    assert obj.cspDomainId == "795e5d76-ee63-4366-b3d4-07bd2ab40250", "obj.cspDomainId ok"

    obj_json['service'] = None
    obj = obj_helper.ClusterObject(obj_json)
    assert obj is not None, "cluster is not None"
    assert obj.raw() is not None, "cluster has data"
    assert obj.raw() == obj_json, "cluster raw == json"
    assert obj.service_type() == "", "cluster service type unknown ok"
    assert obj.service_pod() == "", "cluster service pod unknown ok"
    assert obj.service_state() == "UNKNOWN", "cluster service state ok"
    try:
        print(obj.foo)
    except KeyError:
        pass
    else:
        assert False, "obj.foo should throw"
    obj = obj_helper.ClusterObject()
    assert obj is not None, "empty cluster is not None"
