# Copyright 2019 Tad Lebeck
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

.PHONY: test

venv:
	python3 -m venv venv
	venv/bin/pip3 install --upgrade pip
	venv/bin/pip3 install pylint requests pyyaml pytest boto3

LINT_DIRS=scripts lib tests
lint: venv
	. venv/bin/activate && (find $(LINT_DIRS) -name \*.py | PYTHONPATH=lib xargs -n1 -t pylint --rcfile=testingtools/pylintrc)

test:: venv
	. venv/bin/activate && PYTHONPATH=lib pytest tests -v

clean::
	$(RM) -r venv
