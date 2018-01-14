from __future__ import absolute_import, print_function, unicode_literals

import logging
import os
import json
import pipes
import re
import shutil
import tempfile
import time

import shellescape
from cwltool.draft2tool import CommandLineTool
from cwltool.errors import WorkflowException, UnsupportedRequirement
from cwltool.pathmapper import ensure_writable
from cwltool.stdfsaccess import StdFsAccess
from cwltool.utils import get_feature
from cwltool.workflow import defaultMakeTool
from cwltool.job import relink_initialworkdir, needs_shell_quoting_re
from cwltool.job import stageFiles
from pprint import pformat
from schema_salad.ref_resolver import file_uri

from reana_workflow_engine_cwl.pipeline import Pipeline, PipelineJob
from reana_workflow_engine_cwl.poll import PollThread
from reana_workflow_engine_cwl.httpclient import ReanaJobControllerHTTPClient as HttpClient

log = logging.getLogger("cwl-backend")


class ReanaPipeline(Pipeline):

    def __init__(self, working_dir, kwargs):
        super(ReanaPipeline, self).__init__()
        self.kwargs = kwargs
        self.service = HttpClient()
        if kwargs.get("basedir") is not None:
            self.basedir = kwargs.get("basedir")
        else:
            self.basedir = os.getcwd()
        self.fs_access = StdFsAccess(self.basedir)
        self.working_dir = working_dir

    def make_exec_tool(self, spec, **kwargs):
        return ReanaPipelineTool(spec, self, fs_access=self.fs_access, working_dir=self.working_dir, **kwargs)

    def make_tool(self, spec, **kwargs):
        if "class" in spec and spec["class"] == "CommandLineTool":
            return self.make_exec_tool(spec, **kwargs)
        else:
            return defaultMakeTool(spec, **kwargs)


class ReanaPipelineTool(CommandLineTool):

    def __init__(self, spec, pipeline, fs_access, working_dir, **kwargs):
        super(ReanaPipelineTool, self).__init__(spec, **kwargs)
        self.spec = spec
        self.pipeline = pipeline
        self.fs_access = fs_access
        self.working_dir = working_dir

    def makeJobRunner(self, use_container=True, **kwargs):
        dockerReq, _ = self.get_requirement("DockerRequirement")
        if not dockerReq and use_container:
            if self.find_default_container:
                default_container = self.find_default_container(self)
                if default_container:
                    self.requirements.insert(0, {
                        "class": "DockerRequirement",
                        "dockerPull": default_container
                    })

        return ReanaPipelineJob(self.spec, self.pipeline, self.fs_access, self.working_dir)


class ReanaPipelineJob(PipelineJob):

    def __init__(self, spec, pipeline, fs_access, working_dir):
        super(ReanaPipelineJob, self).__init__(spec, pipeline)
        self.outputs = None
        self.fs_access = fs_access
        self.working_dir = working_dir
        self.inplace_update = False
        self.volumes = []

    def add_volumes(self, pathmapper):

        host_outdir = self.outdir
        container_outdir = self.builder.outdir
        for src, vol in pathmapper.items():
            if not vol.staged:
                continue
            if vol.target.startswith(container_outdir + "/"):
                host_outdir_tgt = os.path.join(
                    host_outdir, vol.target[len(container_outdir) + 1:])
            else:
                host_outdir_tgt = None
            if vol.type in ("File", "Directory"):
                if not vol.resolved.startswith("_:"):
                    self.volumes.append((vol.resolved, vol.target))
            elif vol.type == "WritableFile":
                if self.inplace_update:
                    self.volumes.append((vol.resolved, vol.target))
                else:
                    shutil.copy(vol.resolved, host_outdir_tgt)
                    ensure_writable(host_outdir_tgt)
            if vol.type == "WritableDirectory":
                if vol.resolved.startswith("_:"):
                    if not os.path.exists(vol.target):
                        os.makedirs(vol.target, mode=0o0755)
                else:
                    if self.inplace_update:
                        pass
                    else:
                        shutil.copytree(vol.resolved, host_outdir_tgt)
                        ensure_writable(host_outdir_tgt)
            elif vol.type == "CreateFile":
                if host_outdir_tgt:
                    with open(host_outdir_tgt, "wb") as f:
                        f.write(vol.resolved.encode("utf-8"))
                else:
                    fd, createtmp = tempfile.mkstemp(dir=self.tmpdir)
                    with os.fdopen(fd, "wb") as f:
                        f.write(vol.resolved.encode("utf-8"))

    def parse_job_order(self, k, v, inputs):
        if isinstance(v, dict):
            if all([i in v for i in ["location", "path", "class"]]):
                inputs.append(self.create_input_parameter(k, v))

                if "secondaryFiles" in v:
                    for f in v["secondaryFiles"]:
                        self.parse_job_order(f["basename"], f, inputs)

            else:
                for sk, sv in v.items():
                    if isinstance(sv, dict):
                        self.parse_job_order(sk, sv, inputs)

                    else:
                        break

        elif isinstance(v, list):
            for i in range(len(v)):
                if isinstance(v[i], dict):
                    self.parse_job_order("%s[%s]" % (k, i), v[i], inputs)

                else:
                    break

        return inputs

    def parse_listing(self, listing, inputs):
        for item in listing:

            # TODO:
            if "writable" in item:
                raise UnsupportedRequirement(
                    "The TES spec does not allow for writable inputs"
                    )

            if "contents" in item:
                loc = self.fs_access.join(self.tmpdir, item["basename"])
                with self.fs_access.open(loc, "wb") as gen:
                    gen.write(item["contents"])
            else:
                loc = item["location"]

        return inputs

    def collect_input_parameters(self):
        inputs = []

        # find all primary and secondary input files
        for k, v in self.joborder.items():
            self.parse_job_order(k, v, inputs)

        # manage InitialWorkDirRequirement
        self.parse_listing(self.generatefiles["listing"], inputs)

        return inputs

    def create_task_msg(self):

        container = self.find_docker_requirement()
        requirements_command_line = ""
        for var in self.environment:
                requirements_command_line += "export {0}=\"{1}\";".format(var, self.environment[var])

        if self.volumes:
            for filepair in self.volumes:
                requirements_command_line += "ln -s {0} {1} ;".format(filepair[0], filepair[1])

        mounted_outdir = self.outdir
        if mounted_outdir.startswith("/tmp"):
            mounted_outdir = re.sub("/tmp/.*?/.*?/", self.working_dir + "/", mounted_outdir)
        scr, _ = get_feature(self, "ShellCommandRequirement")

        shebang_lines = {"/bin/bash", "/bin/sh"}
        if scr or self.command_line[0] in shebang_lines:
            shouldquote = lambda x: False
            self.command_line = self.command_line[2:]
        else:
            shouldquote = needs_shell_quoting_re.search
        shellQuote = True
        for b in self.builder.bindings:
            if "shellQuote" in b:
                shellQuote = b.get("shellQuote")
                break



        command_line = " ".join([shellescape.quote(arg) if shouldquote(arg) else  arg for arg in
                                       self.command_line])
        command_line = command_line.replace('/bin/sh -c ', '')
        command_line = re.sub("/var/lib/cwl/.*?/", "/".join(self.working_dir.split("/")[:-1]) + "/workspace/", command_line)
        command_line = re.sub("/tmp/.*?/.*?/", self.working_dir + "/", command_line)
        if self.stdin:
            path = self.stdin.split("/")
            if os.path.isabs(self.stdin):
                command_line = command_line + " < {0}".format(self.stdin)
            else:
                if len(path) > 1:
                    parent_dir = "/".join(mounted_outdir.split("/")[:-1])
                    command_line = command_line + " < {0}".format(os.path.join(parent_dir, path[-1]))
                else:
                    command_line = command_line + " < {0}".format(os.path.join(mounted_outdir, path))
        if self.stdout:
            if os.path.isabs(self.stdout):
                command_line = command_line + " > {0}".format(self.stdout)
            else:
                command_line = command_line + " > {0}".format(os.path.join(self.environment["HOME"], self.stdout))
        if self.stderr:
            if os.path.isabs(self.stderr):
                stderr = self.stderr
            else:
                stderr = os.path.join(mounted_outdir, self.stderr)
            command_line += " 2> " + stderr
            if scr and not shellQuote:
                command_line = command_line.replace("&2", stderr)

        wf_space_cmd = "mkdir -p {0} && cd {0} && ".format(self.environment["HOME"]) + command_line
        wf_space_cmd = requirements_command_line + wf_space_cmd

        docker_output_dir = None
        docker_req, _ = get_feature(self, "DockerRequirement")
        if docker_req:
            docker_output_dir = docker_req.get("dockerOutputDirectory", None)
        if docker_output_dir:
            wf_space_cmd = "mkdir -p {0} && {1} ; cp -r {0} {2}".format(docker_output_dir, wf_space_cmd, mounted_outdir)
        wf_space_cmd += "; cp -r {0}/* {1}".format(self.environment['HOME'], mounted_outdir)
        wrapped_cmd = "/bin/sh -c {} ".format(pipes.quote(wf_space_cmd))

        create_body = {
            "experiment": "default",
            "image": container,
            "cmd": wrapped_cmd
        }

        return create_body

    def run(self, pull_image=True, rm_container=True, rm_tmpdir=True,
            move_outputs="move", **kwargs):

        self._setup(kwargs)

        env = self.environment
        if not os.path.exists(self.tmpdir):
            os.makedirs(self.tmpdir)
        vars_to_preserve = kwargs.get("preserve_environment")
        if kwargs.get("preserve_entire_environment"):
            vars_to_preserve = os.environ
        if vars_to_preserve is not None:
            for key, value in os.environ.items():
                if key in vars_to_preserve and key not in env:
                    env[key] = value
        env["HOME"] = self.builder.outdir
        env["TMPDIR"] = self.tmpdir
        if "PATH" not in env:
            env["PATH"] = os.environ["PATH"]
        if "SYSTEMROOT" not in env and "SYSTEMROOT" in os.environ:
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]

        stageFiles(self.pathmapper, ignoreWritable=True, symLink=True)
        if getattr(self, "generatemapper",""):
            stageFiles(self.generatemapper, ignoreWritable=self.inplace_update, symLink=False)
            relink_initialworkdir(self.generatemapper, self.outdir, self.builder.outdir,
                                  inplace_update=self.inplace_update)
        self.add_volumes(self.pathmapper)
        # if self.generatemapper:
        if getattr(self, "generatemapper",""):
            self.add_volumes(self.generatemapper)

        # useful for debugging
        log.debug(
            "[job %s] self.__dict__ in run() ----------------------" %
            (self.name)
        )
        log.debug(pformat(self.__dict__))

        task = self.create_task_msg()

        log.info(
            "[job %s] CREATED TASK MSG----------------------" %
            (self.name)
        )
        log.info(pformat(task))

        try:
            task_id = self.pipeline.service.submit(**task)
            log.info(
                "[job %s] SUBMITTED TASK ----------------------" %
                (self.name)
            )
            log.info("[job %s] task id: %s " % (self.name, task_id))
            operation = self.pipeline.service.check_status(task_id)
        except Exception as e:
            log.error(
                "[job %s] Failed to submit task to job controller:\n%s" %
                (self.name, e)
            )
            return WorkflowException(e)

        def callback():
            try:
                outputs = self.collect_outputs(self.outdir)
                cleaned_outputs = {}
                for k, v in outputs.items():
                    if isinstance(k, bytes):
                        k = k.decode("utf8")
                    if isinstance(v, bytes):
                        v = v.decode("utf8")
                    cleaned_outputs[k] = v
                self.outputs = cleaned_outputs
                self.output_callback(self.outputs, "success")
            except WorkflowException as e:
                log.error("[job %s] job error:\n%s" % (self.name, e))
                self.output_callback({}, "permanentFail")
            except Exception as e:
                log.error("[job %s] job error:\n%s" % (self.name, e))
                self.output_callback({}, "permanentFail")
            finally:
                if self.outputs is not None:
                    log.info(
                        "[job %s] OUTPUTS ------------------" %
                        (self.name)
                    )
                    log.info(pformat(self.outputs))
                self.cleanup(rm_tmpdir)

        poll = ReanaPipelinePoll(
            jobname=self.name,
            service=self.pipeline.service,
            operation=operation,
            callback=callback
        )

        self.pipeline.add_thread(poll)
        poll.start()

    def cleanup(self, rm_tmpdir):
        log.debug(
            "[job %s] STARTING CLEAN UP ------------------" %
            (self.name)
        )
        if self.stagedir and os.path.exists(self.stagedir):
            log.debug(
                "[job %s] Removing input staging directory %s" %
                (self.name, self.stagedir)
            )
            shutil.rmtree(self.stagedir, True)

        if rm_tmpdir:
            log.debug(
                "[job %s] Removing temporary directory %s" %
                (self.name, self.tmpdir)
            )
            shutil.rmtree(self.tmpdir, True)

    def output2url(self, path):
        if path is not None:
            return file_uri(
                self.fs_access.join(self.outdir, os.path.basename(path))
            )
        return None

    def output2path(self, path):
        if path is not None:
            return self.fs_access.join(self.docker_workdir, path)
        return None


class ReanaPipelinePoll(PollThread):

    def __init__(self, jobname, service, operation, callback):
        super(ReanaPipelinePoll, self).__init__(operation)
        self.name = jobname
        self.service = service
        self.callback = callback

    def run(self):
        while not self.is_done(self.operation):
            time.sleep(self.poll_interval)
            # slow down polling over time till it hits a max
            # if self.poll_interval < 30:
            #     self.poll_interval += 1
            log.debug(
                "[job %s] POLLING %s" %
                (self.name, pformat(self.id))
            )
            try:
                self.operation = self.poll()
            except Exception as e:
                log.error("[job %s] POLLING ERROR %s" % (self.name, e))
                if self.poll_retries > 0:
                    self.poll_retries -= 1
                    continue
                else:
                    log.error("[job %s] MAX POLLING RETRIES EXCEEDED" %
                              (self.name))
                    break

        self.complete(self.operation)

    def poll(self):
        return self.service.check_status(self.id)

    def is_done(self, operation):
        terminal_states = ["succeeded", "failed"]
        if operation['status'] in terminal_states:
            log.info(
                "[job %s] FINAL JOB STATE: %s ------------------" %
                (self.name, operation['status'])
            )
            if operation['status'] != "failed":
                log.error(
                    "[job %s] task id: %s" % (self.name, self.id)
                )
                log.error(
                    "[job %s] logs: %s" %
                    (
                        self.name,
                        self.service.get_logs(self.id)
                    )

                )
            return True
        return False

    def complete(self, operation):
        self.callback()
