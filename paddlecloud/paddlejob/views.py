from django.http import HttpResponseRedirect, HttpResponse, JsonResponse, HttpResponseNotFound, HttpResponseForbidden
from django.contrib import messages
from django.conf import settings
from django.utils.encoding import smart_str
from django.contrib.auth.decorators import login_required
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from . import PaddleJob
from rest_framework.authtoken.models import Token
from rest_framework import viewsets, generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser, FileUploadParser
import json
import utils
import notebook.utils
import logging
import volume
import os
import copy
from notebook.models import FilePublish
import uuid

def file_publish_view(request):
    """
        view for download published files
    """
    username = request.user.username
    publish_uuid = request.GET.get("uuid")
    if not publish_uuid:
        return HttpResponseNotFound()
    record = FilePublish.objects.get(uuid=publish_uuid)
    if not record:
        return HttpResponseNotFound()
    # FIXME(typhoonzero): not support folder currently
    if record.path.endswith("/"):
        return HttpResponseNotFound()

    real_path = "/".join([settings.STORAGE_PATH] + record.path.split("/")[4:])
    logging.info("downloading file from: %s, record(%s)", real_path, record.path)

    # mimetype is replaced by content_type for django 1.7
    response = HttpResponse(open(real_path), content_type='application/force-download') 
    response['Content-Disposition'] = 'attachment; filename=%s' % os.path.basename(record.path)
    # It's usually a good idea to set the 'Content-Length' header too.
    # You can also set any other required headers: Cache-Control, etc.
    return response

class FilePublishAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, format=None):
        """
            return a list of published files for current user
        """
        record = FilePublish.objects.filter(user=request.user)
        file_list = [rec.path for rec in record]
        url_list = [rec.url for rec in record]
        return Response({"files": file_list, "urls": url_list})

    def post(self, request, format=None):
        """
            given a pfs path generate a uniq sharing url for the path
        """
        post_body = json.loads(request.body)
        file_path = post_body.get("path")
        publish_uuid = uuid.uuid4()
        publish_url = "http://%s/filepub/?uuid=%s" % (request.META["HTTP_HOST"], publish_uuid)
        # save publish_url to mysql
        publish_record = FilePublish()
        publish_record.url = publish_url
        publish_record.user = request.user
        publish_record.path = file_path
        publish_record.uuid = publish_uuid
        publish_record.save()
        return Response({"url": publish_url})


class JobsView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, format=None):
        """
        List all jobs
        """
        username = request.user.username
        namespace = notebook.utils.email_escape(username)
        api_instance = client.BatchV1Api(api_client=notebook.utils.get_user_api_client(username))
        job_list = api_instance.list_namespaced_job(namespace)
        # NOTE: when job is deleted, some pods of the job will be at "Terminating" status
        # for a while, which may cause submit fail. Find all pods that are still "Terminating".
        user_pod_list = client.CoreV1Api(api_client=notebook.utils.get_user_api_client(username)).list_namespaced_pod(namespace)
        terminating_jobs = []
        for pod in user_pod_list.items:
            jobname = ""
            if not pod.metadata.labels:
                continue
            if "paddle-job" in pod.metadata.labels:
                jobname = pod.metadata.labels["paddle-job"]
            elif "paddle-job-master" in pod.metadata.labels:
                jobname = pod.metadata.labels["paddle-job-master"]
            elif "paddle-job-pserver" in pod.metadata.labels:
                jobname = pod.metadata.labels["paddle-job-pserver"]
            if pod.metadata.deletion_timestamp and jobname:
                if jobname not in terminating_jobs:
                    terminating_jobs.append(jobname)
        # NOTE: put it in the original dict for backward compability
        ret_dict = copy.deepcopy(job_list.to_dict())
        ret_dict["terminating"] = terminating_jobs
        return Response(ret_dict)

    def post(self, request, format=None):
        """
        Submit the PaddlePaddle job
        """
        username = request.user.username
        namespace = notebook.utils.email_escape(username)
        obj = json.loads(request.body)
        topology = obj.get("topology", "")
        entry = obj.get("entry", "")
        fault_tolerant = obj.get("faulttolerant", False)
        api_client = notebook.utils.get_user_api_client(username)
        if not topology and not entry:
            return utils.simple_response(500, "no topology or entry specified")
        if not obj.get("datacenter"):
            return utils.simple_response(500, "no datacenter specified")
        cfgs = {}
        dc = obj.get("datacenter")

        volumes = []
        for k, cfg in settings.DATACENTERS.items():
            if k != dc and k != "public":
                continue
            fstype = cfg["fstype"]
            if fstype == settings.FSTYPE_CEPHFS:
                if k == "public":
                    mount_path = cfg["mount_path"] % dc
                    cephfs_path = cfg["cephfs_path"]
                else:
                    mount_path = cfg["mount_path"] % (dc, username)
                    cephfs_path = cfg["cephfs_path"] % username
                volumes.append(volume.get_volume_config(
                    fstype = fstype,
                    name = k.replace("_", "-"),
                    monitors_addr = cfg["monitors_addr"],
                    secret = cfg["secret"],
                    user = cfg["user"],
                    mount_path = mount_path,
                    cephfs_path = cephfs_path,
                    admin_key = cfg["admin_key"],
                    read_only = cfg.get("read_only", False)
                ))
            elif fstype == settings.FSTYPE_HOSTPATH:
                if k == "public":
                    mount_path = cfg["mount_path"] % dc
                    host_path = cfg["host_path"]
                else:
                    mount_path = cfg["mount_path"] % (dc, username)
                    host_path = cfg["host_path"] % username

                volumes.append(volume.get_volume_config(
                    fstype = fstype,
                    name = k.replace("_", "-"),
                    mount_path = mount_path,
                    host_path = host_path
                ))
            else:
                pass
        registry_secret = obj.get("registry", None)
        if not registry_secret:
            registry_secret = settings.JOB_DOCKER_IMAGE.get("registry_secret", None)
        # get user specified image
        job_image = obj.get("image", None)
        gpu_count = obj.get("gpu", 0)
        # jobPackage validation: startwith /pfs
        # NOTE: job packages are uploaded to /pfs/[dc]/home/[user]/jobs/[jobname]
        job_name = obj.get("name", "paddle-cluster-job")
        package_in_pod = os.path.join("/pfs/%s/home/%s"%(dc, username), "jobs", job_name)

        logging.info("current package: %s", package_in_pod)
        # package must be ready before submit a job
        current_package_path = package_in_pod.replace("/pfs/%s/home"%dc, settings.STORAGE_PATH)
        if not os.path.exists(current_package_path):
            current_package_path = package_in_pod.replace("/pfs/%s/home/%s"%(dc, username), settings.STORAGE_PATH)
            if not os.path.exists(current_package_path):
                return utils.error_message_response("package not exist in cloud: %s"%current_package_path)
        logging.info("current package in pod: %s", current_package_path)

        # checkout GPU quota
        # TODO(Yancey1989) We should move this to Kubernetes
        if 'GPU_QUOTA' in dir(settings) and int(obj.get('gpu', '0')) > 0:
            gpu_usage = 0
            pods = client.CoreV1Api(api_client=api_client).list_namespaced_pod(namespace=namespace)
            for pod in pods.items:
                # only statistics trainer GPU resource, pserver does not use GPU
                if pod.metadata.labels and 'paddle-job' in pod.metadata.labels and \
                    pod.status.phase == 'Running':
                    gpu_usage += int(pod.spec.containers[0].resources.limits.get('alpha.kubernetes.io/nvidia-gpu', '0'))
            if username in settings.GPU_QUOTA:
                gpu_quota = settings.GPU_QUOTA[username]['limit']
            else:
                gpu_quota = settings.GPU_QUOTA['DEFAULT']['limit']
            gpu_available = gpu_quota - gpu_usage
            gpu_request = int(obj.get('gpu', 0)) * int(obj.get('parallelism', 1))
            print 'gpu available: %d, gpu request: %d' % (gpu_available, gpu_request)
            if gpu_available < gpu_request:
                return utils.error_message_response("You don't have enought GPU quota," + \
                    "request: %d, usage: %d, limit: %d" % (gpu_request, gpu_usage, gpu_quota))

        # use default images
        if not job_image :
            if gpu_count > 0:
                job_image = settings.JOB_DOCKER_IMAGE["image_gpu"]
            else:
                job_image = settings.JOB_DOCKER_IMAGE["image"]

        # add Nvidia lib volume if training with GPU
        if gpu_count > 0:
            volumes.append(volume.get_volume_config(
                fstype = settings.FSTYPE_HOSTPATH,
                name = "nvidia-libs",
                mount_path = "/usr/local/nvidia/lib64",
                host_path = settings.NVIDIA_LIB_PATH
            ))
        envs = {}
        envs.update({"PADDLE_CLOUD_CURRENT_DATACENTER": dc})
        envs.update({"PADDLE_CLOUD_USERNAME": username})
        # ===================== create PaddleJob instance ======================
        paddle_job = PaddleJob(
            name = job_name,
            job_package = package_in_pod,
            parallelism = obj.get("parallelism", 1),
            cpu = obj.get("cpu", 1),
            memory = obj.get("memory", "1Gi"),
            pservers = obj.get("pservers", 1),
            pscpu = obj.get("pscpu", 1),
            psmemory = obj.get("psmemory", "1Gi"),
            topology = topology,
            entry = entry,
            gpu = obj.get("gpu", 0),
            image = job_image,
            passes = obj.get("passes", 1),
            registry_secret = registry_secret,
            volumes = volumes,
            envs = envs,
            fault_tolerant = fault_tolerant,
            etcd_image = settings.ETCD_IMAGE
        )
        # ========== submit master ReplicaSet if using fault_tolerant feature ==
        # FIXME: alpha features in separate module
        if fault_tolerant:
            try:
                ret = client.ExtensionsV1beta1Api(api_client=api_client).create_namespaced_replica_set(
                    namespace,
                    paddle_job.new_master_job())
            except ApiException, e:
                logging.error("error submitting master job: %s", e)
                return utils.simple_response(500, str(e))
        # ========================= submit pserver job =========================
        try:
            ret = client.ExtensionsV1beta1Api(api_client=api_client).create_namespaced_replica_set(
                namespace,
                paddle_job.new_pserver_job())
        except ApiException, e:
            logging.error("error submitting pserver job: %s ", e)
            return utils.simple_response(500, str(e))
        # ========================= submit trainer job =========================
        try:
            ret = client.BatchV1Api(api_client=api_client).create_namespaced_job(
                namespace,
                paddle_job.new_trainer_job())
        except ApiException, e:
            logging.error("error submitting trainer job: %s" % e)
            return utils.simple_response(500, str(e))

        # TODO(typhoonzero): stop master and pservers when job finish or fails

        return utils.simple_response(200, "")

    def delete(self, request, format=None):
        """
        Kill a job
        """
        username = request.user.username
        namespace = notebook.utils.email_escape(username)
        obj = json.loads(request.body)
        jobname = obj.get("jobname")
        api_client = notebook.utils.get_user_api_client(username)
        if not jobname:
            return utils.simple_response(500, "must specify jobname")
        # FIXME: options needed: grace_period_seconds, orphan_dependents, preconditions
        # FIXME: cascade delteing
        delete_status = []
        # delete job
        trainer_name = jobname + "-trainer"
        try:
            u_status = client.BatchV1Api(api_client=api_client)\
                .delete_namespaced_job(trainer_name, namespace, {})
        except ApiException, e:
            logging.error("error deleting job: %s, %s", jobname, str(e))
            delete_status.append(str(e))

        # delete job pods
        try:
            job_pod_list = client.CoreV1Api(api_client=api_client)\
                .list_namespaced_pod(namespace,
                                     label_selector="paddle-job=%s"%jobname)
            for i in job_pod_list.items:
                u_status = client.CoreV1Api(api_client=api_client)\
                    .delete_namespaced_pod(i.metadata.name, namespace, {})
        except ApiException, e:
            logging.error("error deleting job pod: %s", str(e))
            delete_status.append(str(e))

        # delete pserver rs
        pserver_name = jobname + "-pserver"
        try:
            u_status = client.ExtensionsV1beta1Api(api_client=api_client)\
                .delete_namespaced_replica_set(pserver_name, namespace, {})
        except ApiException, e:
            logging.error("error deleting pserver: %s" % str(e))
            delete_status.append(str(e))

        # delete pserver pods
        try:
            # pserver replica set has label with jobname
            job_pod_list = client.CoreV1Api(api_client=api_client)\
                .list_namespaced_pod(namespace,
                                     label_selector="paddle-job-pserver=%s"%jobname)
            for i in job_pod_list.items:
                u_status = client.CoreV1Api(api_client=api_client)\
                    .delete_namespaced_pod(i.metadata.name, namespace, {})
        except ApiException, e:
            logging.error("error deleting pserver pods: %s" % str(e))
            delete_status.append(str(e))

        # delete master rs
        master_name = jobname + "-master"
        try:
            u_status = client.ExtensionsV1beta1Api(api_client=api_client)\
                .delete_namespaced_replica_set(master_name, namespace, {})
        except ApiException, e:
            logging.error("error deleting master: %s" % str(e))
            # just ignore deleting master failed, we do not set up master process
            # without fault tolerant mode
            #delete_status.append(str(e))

        # delete master pods
        try:
            # master replica set has label with jobname
            job_pod_list = client.CoreV1Api(api_client=api_client)\
                .list_namespaced_pod(namespace,
                                     label_selector="paddle-job-master=%s"%jobname)
            for i in job_pod_list.items:
                u_status = client.CoreV1Api(api_client=api_client)\
                    .delete_namespaced_pod(i.metadata.name, namespace, {})
        except ApiException, e:
            logging.error("error deleting master pods: %s" % str(e))
            # just ignore deleting master failed, we do not set up master process
            # without fault tolerant mode
            #delete_status.append(str(e))

        if len(delete_status) > 0:
            retcode = 500
        else:
            retcode = 200
        return utils.simple_response(retcode, "\n".join(delete_status))

class PserversView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, format=None):
        """
        List all pservers
        """
        username = request.user.username
        namespace = notebook.utils.email_escape(username)
        api_instance = client.ExtensionsV1beta1Api(api_client=notebook.utils.get_user_api_client(username))
        pserver_rs_list = api_instance.list_namespaced_replica_set(namespace)
        return Response(pserver_rs_list.to_dict())


class LogsView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, format=None):
        """
        Get logs for jobs
        """
        def _get_pod_log(api_client, namespace, pod_name, num_lines):
            try:
                if num_lines:
                    pod_log = client.CoreV1Api(api_client=api_client)\
                        .read_namespaced_pod_log(
                            pod_name, namespace, tail_lines=int(num_lines))
                else:
                    pod_log = client.CoreV1Api(api_client=api_client)\
                        .read_namespaced_pod_log(i.metadata.name, namespace)
                return pod_log
            except ApiException, e:
                return str(e)

        username = request.user.username
        namespace = notebook.utils.email_escape(username)
        api_client = notebook.utils.get_user_api_client(username)
        jobname = request.query_params.get("jobname")
        num_lines = request.query_params.get("n")
        worker = request.query_params.get("w")
        job_pod_list = client.CoreV1Api(api_client=api_client)\
            .list_namespaced_pod(namespace, label_selector="paddle-job=%s"%jobname)
        total_job_log = ""
        if not worker:
            for i in job_pod_list.items:
                total_job_log = "".join((total_job_log, "==========================%s==========================" % i.metadata.name))
                pod_log = _get_pod_log(api_client, namespace, i.metadata.name, num_lines)
                total_job_log = "\n".join((total_job_log, pod_log))
        else:
            total_job_log = _get_pod_log(api_client, namespace, worker, num_lines)
        return utils.simple_response(200, total_job_log)

class WorkersView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, format=None):
        """
        Get logs for jobs
        """
        username = request.user.username
        namespace = notebook.utils.email_escape(username)
        jobname = request.query_params.get("jobname")
        job_pod_list = None
        api_client = notebook.utils.get_user_api_client(username)
        if not jobname:
            job_pod_list = client.CoreV1Api(api_client=api_client)\
                .list_namespaced_pod(namespace)
        else:
            selector = "paddle-job=%s"%jobname
            job_pod_list = client.CoreV1Api(api_client=api_client)\
                .list_namespaced_pod(namespace, label_selector=selector)
        return Response(job_pod_list.to_dict())

class QuotaView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, format=None):
        """
        Get user quotas
        """
        username = request.user.username
        namespace = notebook.utils.email_escape(username)
        api_client = notebook.utils.get_user_api_client(username)
        quota_list = client.CoreV1Api(api_client=api_client)\
            .list_namespaced_resource_quota(namespace)
        return Response(quota_list.to_dict())

class GetUserView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, format=None):
        """
        Get user name
        """
        content = {
            'user': request.user.username,  # `django.contrib.auth.User` instance.
        }
        return Response(content)

class SimpleFileView(APIView):
    permission_classes = (permissions.IsAuthenticated,)
    parser_classes = (FormParser, MultiPartParser,)

    def __validate_path(self, request, file_path):
        """
        returns error_msg. error_msg will be empty if there's no error.
        """
        path_parts = file_path.split(os.path.sep)

        assert(path_parts[1]=="pfs")
        assert(path_parts[2] in settings.DATACENTERS.keys())
        assert(path_parts[3] == "home")
        assert(path_parts[4] == request.user.username)

        server_file = os.path.join(settings.STORAGE_PATH, request.user.username, *path_parts[5:])

        return server_file

    def get(self, request, format=None):
        """
        Simple get file.
        """
        file_path = request.query_params.get("path")
        try:
            write_file = self.__validate_path(request, file_path)
        except Exception, e:
            return utils.error_message_response("file path not valid: %s"%str(e))

        if not os.path.exists(os.sep+write_file):
            return Response({"msg": "file not exist"})

        response = HttpResponse(open(write_file), content_type='application/force-download')
        response['Content-Disposition'] = 'attachment; filename="%s"' % os.path.basename(write_file)

        return response

    def post(self, request, format=None):
        """
        Simple put file.
        """
        file_obj = request.data['file']
        file_path = request.query_params.get("path")
        if not file_path:
            return utils.error_message_response("must specify path")
        try:
            write_file = self.__validate_path(request, file_path)
        except Exception, e:
            return utils.error_message_response("file path not valid: %s"%str(e))

        if not os.path.exists(os.path.dirname(write_file)):
            try:
                os.makedirs(os.path.dirname(write_file))
            except OSError as exc: # Guard against race condition
                if exc.errno != errno.EEXIST:
                    raise
        # FIXME: always overwrite package files
        with open(write_file, "w") as fn:
            while True:
                data = file_obj.read(4096)
                if not data:
                    break
                fn.write(data)

        return Response({"msg": ""})


class SimpleFileList(APIView):
    permission_classes = (permissions.IsAuthenticated,)
    parser_classes = (FormParser, MultiPartParser,)

    def get(self, request, format=None):
        """
        Simple list files.
        """
        file_path = request.query_params.get("path")
        dc = request.query_params.get("dc")
        # validate list path must be under user's dir
        path_parts = file_path.split(os.path.sep)
        msg = ""
        if len(path_parts) < 5:
            msg = "path must like /pfs/[dc]/home/[user]"
        else:
            if path_parts[1] != "pfs":
                msg = "path must start with /pfs"
            if path_parts[2] not in settings.DATACENTERS.keys():
                msg = "no datacenter "+path_parts[2]
            if path_parts[3] != "home":
                msg = "path must like /pfs/[dc]/home/[user]"
            if path_parts[4] != request.user.username:
                msg = "not a valid user: " + path_parts[4]
        if msg:
            return Response({"msg": msg})

        real_path = file_path.replace("/pfs/%s/home/%s"%(dc, request.user.username), "/pfs/%s"%request.user.username)
        if not os.path.exists(real_path):
            return Response({"msg": "dir not exist"})

        return Response({"msg": "", "items": os.listdir(real_path)})
