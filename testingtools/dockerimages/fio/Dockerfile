# Copyright 2019 Tad Lebeck
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
FROM ubuntu:18.04 

RUN apt-get -y update && apt-get -y install fio wget

VOLUME /datadir
COPY run.sh /opt/run.sh
COPY rw.fio /opt/rw.fio
RUN chmod +x /opt/run.sh
WORKDIR /datadir
CMD ["/opt/run.sh"]
