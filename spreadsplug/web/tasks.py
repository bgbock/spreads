# -*- coding: utf-8 -*-

# Copyright (C) 2014 Johannes Baiter <johannes.baiter@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import division

import copy
import logging
import shutil

import blinker
import requests
from spreads.vendor.confit import Configuration
from spreads.vendor.pathlib import Path

from spreadsplug.web import task_queue
from persistence import get_workflow
from util import find_stick, GeneratorIO

logger = logging.getLogger('spreadsplug.web.tasks')
signals = blinker.Namespace()
on_transfer_started = signals.signal('transfer:started')
on_transfer_progressed = signals.signal('transfer:progressed')
on_transfer_completed = signals.signal('transfer:completed')
on_submit_started = signals.signal('submit:started')
on_submit_progressed = signals.signal('submit:progressed')
on_submit_completed = signals.signal('submit:completed')


@task_queue.task()
def transfer_to_stick(workflow_id):
    stick = find_stick()
    workflow = get_workflow(workflow_id)
    files = list(workflow.path.rglob('*'))
    num_files = len(files)
    # Filter out problematic characters
    clean_name = (workflow.path.name.replace(':', '_')
                                    .replace('/', '_'))
    workflow.step = 'transfer'
    workflow.step_done = False
    try:
        mount = stick.get_dbus_method(
            "FilesystemMount", dbus_interface="org.freedesktop.UDisks.Device")
        mount_point = mount('', [])
        target_path = Path(mount_point)/clean_name
        if target_path.exists():
            shutil.rmtree(unicode(target_path))
        target_path.mkdir()
        signals['transfer:started'].send(workflow)
        for num, path in enumerate(files, 1):
            signals['transfer:progressed'].send(
                workflow, progress=(num/num_files)*0.79, status=path.name)
            target = target_path/path.relative_to(workflow.path)
            if path.is_dir():
                target.mkdir()
            else:
                shutil.copyfile(unicode(path), unicode(target))
    finally:
        if 'mount_point' in locals():
            signals['transfer:progressed'].send(workflow, progress=0.8,
                                                status="Syncing...")
            unmount = stick.get_dbus_method(
                "FilesystemUnmount",
                dbus_interface="org.freedesktop.UDisks.Device")
            unmount([], timeout=1e6)  # dbus-python doesn't know an infinite
                                      # timeout... unmounting sometimes takes a
                                      # long time, since the device has to be
                                      # synced.
        workflow.step_done = True
        signals['transfer:completed'].send(workflow)


@task_queue.task()
def upload_workflow(workflow_id, endpoint, user_config, start_process=False,
                    start_output=False):
    logger.debug("Uploading workflow to postprocessing server")
    workflow = get_workflow(workflow_id)

    # Temporarily write the user-supplied configuration to the bag
    tmp_cfg = workflow.config.with_overlay(user_config)
    tmp_cfg_path = workflow.path/'config.yaml'
    tmp_cfg.dump(filename=tmp_cfg_path,
                 sections=(user_config['plugins'] + ["plugins", "device"]))
    workflow.bag.add_tagfiles(unicode(tmp_cfg_path))

    # Create a zipstream from the workflow-bag
    zstream = workflow.bag.package_as_zipstream(compression=None)
    zstream_copy = copy.deepcopy(zstream)
    zsize = sum(len(x) for x in zstream_copy)

    def zstream_wrapper():
        """ Wrapper around our zstream so we can emit a signal when all data
        has been streamed to the client.
        """
        transferred = 0
        progress = "0.00"
        for data in zstream:
            yield data
            transferred += len(data)
            # Only update progress if we've progress at least by 0.01
            new_progress = "{0:.2f}".format(transferred/zsize)
            if new_progress != progress:
                progress = new_progress
                signals['submit:progressed'].send(
                    workflow, progress=float(progress),
                    status="Uploading workflow...")

    # NOTE: This is neccessary since requests makes a chunked upload when
    #       passed a plain generator, which is not supported by the WSGI
    #       protocol that receives it. Hence we wrap it inside of a
    #       GeneratorIO to make it appear as a file-like object with a
    #       known size.
    zstream_fp = GeneratorIO(zstream_wrapper(), zsize)
    signals['submit:started'].send(workflow)
    resp = requests.post(endpoint, data=zstream_fp,
                         headers={'Content-Type': 'application/zip'})
    if not resp:
        error_msg = "Upload failed: {0}".format(resp.content)
        signals['submit:error'].send(workflow, message=error_msg,
                                     data=resp.content)
        logger.error(error_msg)
    else:
        wfid = resp.json()['id']
        if start_process:
            requests.post(endpoint + "/{0}/process".format(wfid))
        if start_output:
            requests.post(endpoint + "/{0}/output".format(wfid))
        signals['submit:completed'].send(workflow, remote_id=wfid)

    # Remove configuration file again, since it does not match the scanner
    # configuration/plugin selection
    workflow.remove_tagfiles(tmp_cfg_path)


@task_queue.task()
def process_workflow(workflow_id):
    logger.debug("Initiating processing for workflow {0}"
                 .format(workflow_id))
    workflow = get_workflow(workflow_id)
    workflow.process()


@task_queue.task()
def output_workflow(workflow_id):
    logger.debug("Initiating output generation for workflow {0}"
                 .format(workflow_id))
    workflow = get_workflow(workflow_id)
    workflow.output()
