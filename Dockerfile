# This file is part of REANA.
# Copyright (C) 2017 CERN.
#
# REANA is free software; you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# REANA is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# REANA; if not, write to the Free Software Foundation, Inc., 59 Temple Place,
# Suite 330, Boston, MA 02111-1307, USA.
#
# In applying this license, CERN does not waive the privileges and immunities
# granted to it by virtue of its status as an Intergovernmental Organization or
# submit itself to any jurisdiction.

FROM python:2.7
ADD . /code
WORKDIR /code
RUN apt update && \
    apt install -y vim emacs-nox && \
    apt install nodejs -y && \
    pip install --upgrade pip && \
    pip install -e .[all] && \
    pip install wdb

ARG QUEUE_ENV=default
ENV QUEUE_ENV ${QUEUE_ENV}
ENV WDB_SOCKET_SERVER=wdb
ENV WDB_NO_BROWSER_AUTO_OPEN=True
ENV TERM=xterm
ENV FLASK_DEBUG=1
ENV PYTHONPATH=/workdir
CMD celery -A reana_workflow_engine_cwl.celeryapp worker -l info -Q ${QUEUE_ENV}
