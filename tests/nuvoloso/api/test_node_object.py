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

Node object helper tests
"""

import json
import os.path
from nuvoloso.api import obj_helper, nuvo_management

def test_node_object():
    """Test NodeObject methods"""
    data_dir = os.path.dirname(os.path.abspath(__file__))
    with open(data_dir+"/data/node.json") as data_fp:
        obj_json = json.load(data_fp)
        obj = obj_helper.NodeObject(obj_json)
    assert obj is not None, "node is not None"
    assert obj.raw() is not None, "node has data"
    assert obj.raw() == obj_json, "node raw == json"
    assert obj.url_type() == nuvo_management.NODES
    assert obj.meta_id() == '5ec2be07-b88f-4c19-b53c-465156df1f08', "obj.meta_id() ok"
    assert obj.meta_version() == 338, "obj.meta_version() ok"
    assert obj.aws_instance() == "i-0bec6fceb53850310", "aws instance ok"
    assert obj.local_hostname() == "ip-172-20-41-49.us-west-2.compute.internal", "node local hostname ok"
    assert obj.public_hostname() == "ec2-34-217-104-224.us-west-2.compute.amazonaws.com", "node public hostname ok"
    assert obj.service_type() == "agentd", "node service type ok"
    assert obj.service_pod() == "nuvoloso-node-h7lv8", "node service pod ok"
    assert obj.service_state() == "READY", "node service state ok"
    assert obj.service_cooked_state() == "MANAGED/READY", "node service state ok"
    assert obj.service_messages() is not None, "node service messages ok"
    assert isinstance(obj.service_messages(), list), "node service messages is a list"
    assert obj.name == "NODE_NAME:ip-172-20-41-49.us-west-2.compute.internal", "obj.name ok"
    assert obj.state == "MANAGED", "obj.state ok"
    assert obj.clusterId == "1bbed242-1ce6-45e6-8172-aa1e370ccb9f", "obj.clusterId ok"
    assert obj.tags is not None, "obj.tags ok"
    assert isinstance(obj.tags, list), "obj.tags is a list"

    obj_json['service'] = None
    obj_json['nodeAttributes'] = None
    obj = obj_helper.NodeObject(obj_json)
    assert obj is not None, "node is not None"
    assert obj.raw() is not None, "node has data"
    assert obj.raw() == obj_json, "node raw == json"
    assert obj.aws_instance() == "", "aws instance ok"
    assert obj.local_hostname() == "", "node local hostname unknown ok"
    assert obj.public_hostname() == "", "node public hostname unknown ok"
    assert obj.service_type() == "", "node service type unknown unknown ok"
    assert obj.service_pod() == "", "node service pod unknown ok"
    assert obj.service_state() == "UNKNOWN", "node service state ok"
    try:
        print(obj.foo)
    except KeyError:
        pass
    else:
        assert False, "obj.foo should throw"
    obj = obj_helper.NodeObject()
    assert obj is not None, "empty node is not None"
