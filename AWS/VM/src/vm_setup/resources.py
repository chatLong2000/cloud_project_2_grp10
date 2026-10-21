"""
AWS resources of the Monitoring & Analytics VM, created idempotently.

Every ensure_* function first looks the resource up (by name or tag) and only
creates what is missing, so each notebook cell can be run again safely.
Security choices: no inbound rule at all (access through SSM Session Manager),
IMDSv2 only with a hop limit of 1 (containers cannot reach the instance
credentials), encrypted gp3 volume, no SSH key, instance role limited to SSM
plus reading the bundle and the Grafana password.
"""

import hashlib
import json
import logging
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from botocore.exceptions import ClientError

from vm_setup import scripts

log = logging.getLogger("vm_setup")

PROJECT_TAG = "cloud-project-2-grp10"
AMI_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
SSM_MANAGED_POLICY = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
INLINE_POLICY_NAME = "coffee-vm-bundle-and-secret"
LIVE_STATES = ["pending", "running", "stopping", "stopped"]
PROFILE_RETRIES = 10  # a new instance profile takes a few seconds to be usable by EC2
PROFILE_RETRY_S = 6
SG_DELETE_RETRIES = 12  # the network interface of a terminated instance is released asynchronously
PENDING_COMMAND_STATES = {"Pending", "InProgress", "Delayed"}

Sleep = Callable[[float], None]
Clock = Callable[[], float]


class SetupError(RuntimeError):
    pass


class CommandError(SetupError):
    pass


@dataclass(frozen=True)
class VmConfig:
    region: str
    account_id: str
    prefix: str = "coffee-vm"
    instance_type: str = "t3.small"
    volume_gb: int = 20
    bundle_key: str = "bundle/app.tar.gz"

    @property
    def parameter_name(self) -> str:
        return f"/{self.prefix}/grafana-admin-password"

    @property
    def role_name(self) -> str:
        return f"{self.prefix}-role"  # the instance profile has the same name

    @property
    def security_group_name(self) -> str:
        return f"{self.prefix}-sg"

    @property
    def instance_name(self) -> str:
        return self.prefix

    @property
    def bucket(self) -> str:
        return f"{self.prefix}-{self.account_id}"  # bucket names are global

    @property
    def parameter_arn(self) -> str:
        return f"arn:aws:ssm:{self.region}:{self.account_id}:parameter{self.parameter_name}"

    @property
    def tags(self) -> dict[str, str]:
        return {"Project": PROJECT_TAG, "Component": "vm"}

    def tag_list(self, **extra: str) -> list[dict[str, str]]:
        return [{"Key": k, "Value": v} for k, v in {**extra, **self.tags}.items()]


class Clients(NamedTuple):
    ec2: Any
    iam: Any
    s3: Any
    ssm: Any

    @classmethod
    def from_session(cls, session: Any) -> "Clients":
        return cls(*(session.client(name) for name in cls._fields))


def _error_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


# --- Grafana admin password -------------------------------------------------


def ensure_admin_password(ssm: Any, cfg: VmConfig) -> bool:
    """Creates the SecureString parameter if missing (never overwrites it). True if created."""
    try:
        ssm.get_parameter(Name=cfg.parameter_name)
        return False
    except ssm.exceptions.ParameterNotFound:
        pass
    ssm.put_parameter(
        Name=cfg.parameter_name,
        Description="Grafana admin password of the coffee VM",
        Value=secrets.token_urlsafe(24),
        Type="SecureString",
        Tier="Standard",
        Tags=cfg.tag_list(),
    )
    return True


# --- S3 bundle bucket ---------------------------------------------------------


def _tls_only_policy(bucket: str) -> dict:
    statement = {
        "Sid": "DenyInsecureTransport",
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:*",
        "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
    }
    return {"Version": "2012-10-17", "Statement": [statement]}


def ensure_bucket(s3: Any, cfg: VmConfig) -> str:
    """Private, encrypted, TLS-only bucket for the deployment bundle."""
    try:
        s3.head_bucket(Bucket=cfg.bucket, ExpectedBucketOwner=cfg.account_id)
    except ClientError as exc:
        if _error_code(exc) == "403":
            raise SetupError(f"bucket {cfg.bucket} exists in another account: change VmConfig.prefix") from None
        if _error_code(exc) not in {"404", "NoSuchBucket"}:
            raise
        location = (
            {} if cfg.region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": cfg.region}}
        )
        s3.create_bucket(Bucket=cfg.bucket, **location)
    s3.put_public_access_block(
        Bucket=cfg.bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=cfg.bucket,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )
    s3.put_bucket_policy(Bucket=cfg.bucket, Policy=json.dumps(_tls_only_policy(cfg.bucket)))
    s3.put_bucket_tagging(Bucket=cfg.bucket, Tagging={"TagSet": cfg.tag_list()})
    return cfg.bucket


def upload_bundle(s3: Any, cfg: VmConfig, data: bytes) -> str:
    """Uploads the bundle to cfg.bundle_key; returns its SHA-256, checked on the VM before deployment."""
    s3.put_object(
        Bucket=cfg.bucket,
        Key=cfg.bundle_key,
        Body=data,
        ServerSideEncryption="AES256",
        ExpectedBucketOwner=cfg.account_id,
    )
    return hashlib.sha256(data).hexdigest()


# --- IAM instance role ------------------------------------------------------------


def _trust_policy() -> dict:
    statement = {"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}
    return {"Version": "2012-10-17", "Statement": [statement]}


def _inline_policy(cfg: VmConfig) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{cfg.bucket}/{cfg.bundle_key}"},
            {"Effect": "Allow", "Action": "ssm:GetParameter", "Resource": cfg.parameter_arn},
        ],
    }


def ensure_instance_role(iam: Any, cfg: VmConfig) -> str:
    """Role + instance profile of the VM; returns the instance profile ARN. Policies are kept up to date."""
    try:
        iam.get_role(RoleName=cfg.role_name)
        iam.update_assume_role_policy(RoleName=cfg.role_name, PolicyDocument=json.dumps(_trust_policy()))
    except iam.exceptions.NoSuchEntityException:
        iam.create_role(
            RoleName=cfg.role_name,
            AssumeRolePolicyDocument=json.dumps(_trust_policy()),
            Description="Coffee VM: SSM access, bundle download, Grafana password",
            Tags=cfg.tag_list(),
        )
    iam.attach_role_policy(RoleName=cfg.role_name, PolicyArn=SSM_MANAGED_POLICY)
    iam.put_role_policy(
        RoleName=cfg.role_name, PolicyName=INLINE_POLICY_NAME, PolicyDocument=json.dumps(_inline_policy(cfg))
    )

    try:
        profile = iam.get_instance_profile(InstanceProfileName=cfg.role_name)["InstanceProfile"]
    except iam.exceptions.NoSuchEntityException:
        profile = iam.create_instance_profile(InstanceProfileName=cfg.role_name, Tags=cfg.tag_list())["InstanceProfile"]
    if cfg.role_name not in {r["RoleName"] for r in profile.get("Roles", [])}:
        iam.add_role_to_instance_profile(InstanceProfileName=cfg.role_name, RoleName=cfg.role_name)
    return profile["Arn"]


# --- Network ---------------------------------------------------------------------------


def default_vpc_id(ec2: Any) -> str:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        raise SetupError("no default VPC in this region: create one (aws ec2 create-default-vpc) or change REGION")
    return vpcs[0]["VpcId"]


def _find_security_group(ec2: Any, cfg: VmConfig, vpc_id: str | None = None) -> dict | None:
    filters = [{"Name": "group-name", "Values": [cfg.security_group_name]}]
    if vpc_id:
        filters.append({"Name": "vpc-id", "Values": [vpc_id]})
    groups = ec2.describe_security_groups(Filters=filters)["SecurityGroups"]
    return groups[0] if groups else None


def ensure_security_group(ec2: Any, cfg: VmConfig, vpc_id: str) -> str:
    """Security group without any inbound rule (rules added by hand are removed)."""
    group = _find_security_group(ec2, cfg, vpc_id)
    if group is None:
        group_id = ec2.create_security_group(
            GroupName=cfg.security_group_name,
            Description="Coffee VM: no inbound access, SSM Session Manager only",
            VpcId=vpc_id,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": cfg.tag_list(Name=cfg.security_group_name)}],
        )["GroupId"]
        return group_id
    if group["IpPermissions"]:
        ec2.revoke_security_group_ingress(GroupId=group["GroupId"], IpPermissions=group["IpPermissions"])
        log.warning("removed inbound rules from %s", group["GroupId"])
    return group["GroupId"]


# --- Instance ----------------------------------------------------------------------------


def latest_ami(ssm: Any) -> str:
    """Latest Amazon Linux 2023 x86_64 AMI of the region."""
    return ssm.get_parameter(Name=AMI_PARAMETER)["Parameter"]["Value"]


def find_instance(ec2: Any, cfg: VmConfig) -> dict | None:
    filters = [
        {"Name": "tag:Name", "Values": [cfg.instance_name]},
        {"Name": "tag:Project", "Values": [PROJECT_TAG]},
        {"Name": "instance-state-name", "Values": LIVE_STATES},
    ]
    instances = [i for r in ec2.describe_instances(Filters=filters)["Reservations"] for i in r["Instances"]]
    if len(instances) > 1:
        ids = ", ".join(i["InstanceId"] for i in instances)
        raise SetupError(f"several instances are tagged {cfg.instance_name}: {ids}; terminate the extra ones")
    return instances[0] if instances else None


def _launch_request(cfg: VmConfig, ami_id: str, security_group_id: str, instance_profile_arn: str, user_data: str):
    return {
        "ImageId": ami_id,
        "InstanceType": cfg.instance_type,
        "MinCount": 1,
        "MaxCount": 1,
        "SecurityGroupIds": [security_group_id],
        "IamInstanceProfile": {"Arn": instance_profile_arn},
        "UserData": user_data,
        "MetadataOptions": {"HttpTokens": "required", "HttpEndpoint": "enabled", "HttpPutResponseHopLimit": 1},
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {
                    "VolumeSize": cfg.volume_gb,
                    "VolumeType": "gp3",
                    "Encrypted": True,
                    "DeleteOnTermination": True,
                },
            }
        ],
        "TagSpecifications": [
            {"ResourceType": kind, "Tags": cfg.tag_list(Name=cfg.instance_name)} for kind in ("instance", "volume")
        ],
    }


def ensure_instance(
    ec2: Any,
    cfg: VmConfig,
    *,
    ami_id: str,
    security_group_id: str,
    instance_profile_arn: str,
    user_data: str,
    sleep: Sleep = time.sleep,
) -> str:
    """
    Instance id: the existing instance (realigned on the given security group and
    instance profile, started if stopped) or a new one. An existing instance keeps
    its AMI and user-data: recreate it (cleanup) to change them.
    """
    existing = find_instance(ec2, cfg)
    if existing is not None:
        _realign_instance(ec2, existing, security_group_id, instance_profile_arn)
        state = existing["State"]["Name"]
        if state == "stopping":
            ec2.get_waiter("instance_stopped").wait(InstanceIds=[existing["InstanceId"]])
        if state in {"stopping", "stopped"}:
            ec2.start_instances(InstanceIds=[existing["InstanceId"]])
        return existing["InstanceId"]

    request = _launch_request(cfg, ami_id, security_group_id, instance_profile_arn, user_data)
    for attempt in range(1, PROFILE_RETRIES + 1):
        try:
            return ec2.run_instances(**request)["Instances"][0]["InstanceId"]
        except ClientError as exc:
            message = exc.response.get("Error", {}).get("Message", "")
            if _error_code(exc) != "InvalidParameterValue" or "instance profile" not in message.lower():
                raise
            if attempt == PROFILE_RETRIES:
                raise SetupError(f"instance profile still not usable after {attempt} attempts: {message}") from None
            sleep(PROFILE_RETRY_S)
    raise AssertionError("unreachable")


def _realign_instance(ec2: Any, instance: dict, security_group_id: str, instance_profile_arn: str) -> None:
    """Puts back the expected security group and instance profile (e.g. after a partial cleanup)."""
    instance_id = instance["InstanceId"]
    if {g["GroupId"] for g in instance.get("SecurityGroups", [])} != {security_group_id}:
        ec2.modify_instance_attribute(InstanceId=instance_id, Groups=[security_group_id])
        log.warning("%s: security group reset to %s", instance_id, security_group_id)
    if instance.get("IamInstanceProfile", {}).get("Arn") == instance_profile_arn:
        return
    associations = ec2.describe_iam_instance_profile_associations(
        Filters=[{"Name": "instance-id", "Values": [instance_id]}, {"Name": "state", "Values": ["associated"]}]
    )["IamInstanceProfileAssociations"]
    profile = {"Arn": instance_profile_arn}
    if associations:
        ec2.replace_iam_instance_profile_association(
            IamInstanceProfile=profile, AssociationId=associations[0]["AssociationId"]
        )
    else:
        ec2.associate_iam_instance_profile(IamInstanceProfile=profile, InstanceId=instance_id)
    log.warning("%s: instance profile reset to %s", instance_id, instance_profile_arn)


def wait_running(ec2: Any, instance_id: str) -> None:
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 5, "MaxAttempts": 60})


def set_running(ec2: Any, instance_id: str, running: bool) -> None:
    if running:
        ec2.start_instances(InstanceIds=[instance_id])
    else:
        ec2.stop_instances(InstanceIds=[instance_id])


# --- SSM -----------------------------------------------------------------------------------


def wait_ssm_online(
    ssm: Any,
    instance_id: str,
    timeout_s: float = 600,
    poll_s: float = 10,
    sleep: Sleep = time.sleep,
    clock: Clock = time.monotonic,
) -> None:
    deadline = clock() + timeout_s
    while True:
        info = ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [instance_id]}])
        pings = [i.get("PingStatus") for i in info["InstanceInformationList"]]
        if "Online" in pings:
            return
        if clock() + poll_s > deadline:
            raise SetupError(
                f"SSM agent of {instance_id} not online after {timeout_s:.0f} s: check that the instance has a "
                "public IP or a route to the SSM endpoints, and that its role has AmazonSSMManagedInstanceCore"
            )
        sleep(poll_s)


def run_command(
    ssm: Any,
    instance_id: str,
    commands: Sequence[str],
    timeout_s: int = 900,
    poll_s: float = 5,
    sleep: Sleep = time.sleep,
    clock: Clock = time.monotonic,
) -> str:
    """Runs a shell script on the instance through SSM; returns its stdout, raises CommandError otherwise."""
    command_id = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": list(commands), "executionTimeout": [str(timeout_s)]},
        Comment="coffee VM setup",
    )["Command"]["CommandId"]
    deadline = clock() + timeout_s + 60
    while True:
        try:
            invocation = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ClientError as exc:
            if _error_code(exc) != "InvocationDoesNotExist":
                raise
            invocation = {"Status": "Pending"}
        status = invocation["Status"]
        if status == "Success":
            return invocation.get("StandardOutputContent", "")
        if status not in PENDING_COMMAND_STATES:
            stderr = invocation.get("StandardErrorContent", "").strip()[-2000:]
            raise CommandError(f"command {command_id} {status} on {instance_id}: {stderr or '(no stderr)'}")
        if clock() + poll_s > deadline:
            raise CommandError(f"command {command_id} timed out on {instance_id} after {timeout_s} s")
        sleep(poll_s)


def wait_healthy(
    ssm: Any,
    instance_id: str,
    timeout_s: float = 600,
    poll_s: float = 15,
    sleep: Sleep = time.sleep,
    clock: Clock = time.monotonic,
    report: Callable[[str], None] = print,
) -> scripts.Status:
    """Polls container health until both services are healthy and Grafana answers."""
    deadline = clock() + timeout_s
    while True:
        output = run_command(ssm, instance_id, scripts.status_commands(), timeout_s=60, sleep=sleep, clock=clock)
        status = scripts.parse_status(output)
        report(str(status))
        if status.ready:
            return status
        if clock() + poll_s > deadline:
            raise SetupError(f"services not healthy after {timeout_s:.0f} s: {status}")
        sleep(poll_s)


# --- Cleanup ------------------------------------------------------------------------------------


def _delete_security_group(ec2: Any, group_id: str, sleep: Sleep) -> None:
    for attempt in range(1, SG_DELETE_RETRIES + 1):
        try:
            ec2.delete_security_group(GroupId=group_id)
            return
        except ClientError as exc:
            if _error_code(exc) != "DependencyViolation" or attempt == SG_DELETE_RETRIES:
                raise
            sleep(10)


def _ignore_missing(iam: Any, call: Callable[..., Any], **kwargs: Any) -> None:
    try:
        call(**kwargs)
    except iam.exceptions.NoSuchEntityException:
        pass


def _delete_role(iam: Any, cfg: VmConfig) -> bool:
    """Instance profile, policies and role; each step tolerates what an earlier partial cleanup removed."""
    name = cfg.role_name
    _ignore_missing(iam, iam.remove_role_from_instance_profile, InstanceProfileName=name, RoleName=name)
    try:
        iam.delete_instance_profile(InstanceProfileName=name)
        removed = True
    except iam.exceptions.NoSuchEntityException:
        removed = False
    try:
        iam.get_role(RoleName=name)
    except iam.exceptions.NoSuchEntityException:
        return removed
    _ignore_missing(iam, iam.detach_role_policy, RoleName=name, PolicyArn=SSM_MANAGED_POLICY)
    _ignore_missing(iam, iam.delete_role_policy, RoleName=name, PolicyName=INLINE_POLICY_NAME)
    iam.delete_role(RoleName=name)
    return True


def _delete_bucket(s3: Any, cfg: VmConfig) -> bool:
    try:
        s3.head_bucket(Bucket=cfg.bucket, ExpectedBucketOwner=cfg.account_id)
    except ClientError as exc:
        if _error_code(exc) in {"404", "NoSuchBucket"}:
            return False
        raise
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=cfg.bucket):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=cfg.bucket, Delete={"Objects": keys})
    s3.delete_bucket(Bucket=cfg.bucket)
    return True


def cleanup(clients: Clients, cfg: VmConfig, sleep: Sleep = time.sleep) -> list[str]:
    """Deletes every resource of the VM; returns what was deleted (empty when nothing was left)."""
    removed = []
    instance = find_instance(clients.ec2, cfg)
    if instance is not None:
        clients.ec2.terminate_instances(InstanceIds=[instance["InstanceId"]])
        clients.ec2.get_waiter("instance_terminated").wait(InstanceIds=[instance["InstanceId"]])
        removed.append(f"instance {instance['InstanceId']}")
    group = _find_security_group(clients.ec2, cfg)
    if group is not None:
        _delete_security_group(clients.ec2, group["GroupId"], sleep)
        removed.append(f"security group {group['GroupId']}")
    if _delete_role(clients.iam, cfg):
        removed.append(f"role + instance profile {cfg.role_name}")
    if _delete_bucket(clients.s3, cfg):
        removed.append(f"bucket {cfg.bucket}")
    try:
        clients.ssm.delete_parameter(Name=cfg.parameter_name)
        removed.append(f"parameter {cfg.parameter_name}")
    except clients.ssm.exceptions.ParameterNotFound:
        pass
    return removed
