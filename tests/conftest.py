import json
import pprint
import os

import docker
import pytest


def pytest_runtest_logreport(report):
    if report.failed:
        client = docker.Client('unix://var/run/docker.sock', version="auto")
        test_containers = client.containers(
            all=True,
            filters={"label": "ceph/daemon"})
        for container in test_containers:
            log_lines = [
                ("docker inspect {!r}:".format(container['Id'])),
                (pprint.pformat(client.inspect_container(container['Id']))),
                ("docker logs {!r}:".format(container['Id'])),
                (client.logs(container['Id'])),
            ]
            report.longrepr.addsection('docker logs', os.linesep.join(log_lines))


def pull_image(image, client):
    """
    Pull the specified image using docker-py

    This function will parse the result from docker-py and raise an exception
    if there is an error.
    """
    response = client.pull(image)
    lines = [line for line in response.splitlines() if line]

    # The last line of the response contains the overall result of the pull
    # operation.
    pull_result = json.loads(lines[-1])
    if "error" in pull_result:
        raise Exception("Could not pull {}: {}".format(
            image, pull_result["error"]))


def generate_ips(start_ip, end_ip=None, offset=None):
    ip_range = []

    start = list(map(int, start_ip.split(".")))
    if offset:
        end = start[-1] + offset
        if end > 255:
            end = 255
        start = start[:-1] + [end]
    else:
        ip_range.append(start_ip)
    if not end_ip:
        end = start[:-1] + [255]
    else:
        end = list(map(int, end_ip.split(".")))
    temp = start


    while temp != end:
        start[3] += 1
        for i in (3, 2, 1):
            if temp[i] == 256:
                temp[i] = 0
                temp[i-1] += 1
        ip_range.append(".".join(map(str, temp)))

    return ip_range


def teardown_container(client, container, container_network):
    client.remove_container(
        container=container['Id'],
        force=True
    )
    client.remove_network(container_network['Id'])


def start_container(client, container, container_network):
    """
    Start a container, wait for (successful) completion of entrypoint
    and raise an exception with container logs otherwise
    """
    try:
        client.start(container=container["Id"])
    except Exception as err:
        teardown_container(client, container, container_network)
        raise
    else:
        # this is non-ideal, we can't tell for sure when the container is really really up
        # after the entry point script has run
        import time;time.sleep(0.5)
        if client.inspect_container(container)['State']['ExitCode'] > 0:
            print "[ERROR][setup] failed to setup container for %s" %  request.param
            for line in client.logs(container, stream=True):
                print "[ERROR][setup]", line.strip('\n')
            raise RuntimeError()
        return container


def remove_container(client, container_name):
    # remove any existing test container
    for test_container in client.containers(all=True):
        for name in test_container['Names']:
            if container_name in name:
                client.remove_container(container=test_container['Id'], force=True)


def remove_container_network(client, container_network_name):
    # now remove any network associated with the containers
    for network in client.networks():
        if network['Name'] == container_network_name:
            client.remove_network(network['Id'])


def create_mon_container(client, container_tag):
    pull_image(container_tag, client)
    # These subnets and gateways are made up. It is *really* hard to come up
    # with a sane gateway/subnet/IP to programmatically set it for the
    # container(s)
    subnet = '172.172.172.0/16'

    # XXX This only generates a single IP, it is useful as-is because when this
    # setup wants to create multiple containers it can easily get a range of
    # IP's for the given subnet
    container_ip = generate_ips('172.172.172.1', offset=1)[-1]

    ipam_pool = docker.utils.create_ipam_pool(
        subnet='172.172.172.0/16',
        gateway='172.172.172.1'
    )

    ipam_config = docker.utils.create_ipam_config(
        pool_configs=[ipam_pool]
    )

    # create the network for the monitor, using the bridge driver
    container_network = client.create_network(
        "pytest_monitor",
        driver="bridge",
        internal=True,
        ipam=ipam_config
    )

    # now map it as part of the networking configuration
    networking_config = client.create_networking_config({
		'pytest_monitor': client.create_endpoint_config(ipv4_address=container_ip)
	})

    # "create" the container, which really doesn't create an actual image, it
    # basically constructs the object needed to start one. This is a 2-step
    # process (equivalent to 'docker run'). It also uses the
    # `networking_config` and `container_network` created above. These are
    # needed because the requirement for the Ceph containers is to know the IP
    # and the subnet(s) beforehand.
    container = client.create_container(
        image=container_tag,
        name='pytest_ceph_mon',
        environment={'CEPH_DAEMON': 'MON', 'MON_IP': container_ip, 'CEPH_PUBLIC_NETWORK': subnet},
        detach=True,
        networking_config=networking_config,
        command='ceph/daemon mon'
    )

    return container, container_network


@pytest.fixture(scope='session')
def client():
    return docker.Client('unix://var/run/docker.sock', version="auto")

container_tags = [
    'ceph/daemon:tag-build-master-hammer-centos-7',
    'ceph/daemon:tag-build-master-infernalis-centos-7',
    'ceph/daemon:tag-build-master-jewel-centos-7',
    'ceph/daemon:tag-build-master-hammer-ubuntu-16.04',
    'ceph/daemon:tag-build-master-infernalis-ubuntu-16.04',
    'ceph/daemon:tag-build-master-jewel-ubuntu-16.04',
    'ceph/daemon:tag-build-master-hammer-ubuntu-14.04',
    'ceph/daemon:tag-build-master-infernalis-ubuntu-14.04',
    'ceph/daemon:tag-build-master-jewel-ubuntu-14.04',
    'ceph/daemon:tag-build-master-jewel-fedora-23',
    'ceph/daemon:tag-build-master-jewel-fedora-24'
]

hammer_tags = [t for t in container_tags if 'hammer' in t]
jewel_tags = [t for t in container_tags if 'jewel' in t]
infernalis_tags = [t for t in container_tags if 'infernalis' in t]


@pytest.fixture(scope='class', params=hammer_tags)
def hammer_containers(client, request):
    # XXX these are using 'mon' names, we need to cleanup when
    # adding tests for OSDs
    pull_image(request.param, client)
    remove_container(client, 'pytest_ceph_mon')
    remove_container_network(client, 'pytest_monitor')
    container, container_network = create_mon_container(client, request.param)
    start_container(client, container, container_network)

    yield container

    teardown_container(client, container, container_network)


@pytest.fixture(scope='class', params=jewel_tags)
def jewel_containers(client, request):
    # XXX these are using 'mon' names, we need to cleanup when
    # adding tests for OSDs
    pull_image(request.param, client)
    remove_container(client, 'pytest_ceph_mon')
    remove_container_network(client, 'pytest_monitor')
    container, container_network = create_mon_container(client, request.param)
    start_container(client, container, container_network)

    yield container

    teardown_container(client, container, container_network)


@pytest.fixture(scope='class', params=infernalis_tags)
def infernalis_containers(client, request):
    # XXX these are using 'mon' names, we need to cleanup when
    # adding tests for OSDs
    pull_image(request.param, client)
    remove_container(client, 'pytest_ceph_mon')
    remove_container_network(client, 'pytest_monitor')
    container, container_network = create_mon_container(client, request.param)
    start_container(client, container, container_network)

    yield container

    teardown_container(client, container, container_network)


@pytest.fixture(scope='class', params=container_tags)
def mon_containers(client, request):
    # XXX there is lots of duplication here with the helpers, this needs to get
    # cleaned up
    pull_image(request.param, client)
    # These subnets and gateways are made up. It is *really* hard to come up
    # with a sane gateway/subnet/IP to programmatically set it for the
    # container(s)
    subnet = '172.172.172.0/16'

    # XXX This only generates a single IP, it is useful as-is because when this
    # setup wants to create multiple containers it can easily get a range of
    # IP's for the given subnet
    container_ip = generate_ips('172.172.172.1', offset=1)[-1]

    ipam_pool = docker.utils.create_ipam_pool(
        subnet='172.172.172.0/16',
        gateway='172.172.172.1'
    )

    ipam_config = docker.utils.create_ipam_config(
        pool_configs=[ipam_pool]
    )

    # remove any existing test monitor container
    for test_container in client.containers(all=True):
        for name in test_container['Names']:
            if 'pytest_ceph_mon' in name:
                client.remove_container(container=test_container['Id'], force=True)

    # now remove any network associated with the containers
    for network in client.networks():
        if network['Name'] == 'pytest_monitor':
            client.remove_network(network['Id'])

    # create the network for the monitor, using the bridge driver
    container_network = client.create_network(
        "pytest_monitor",
        driver="bridge",
        internal=True,
        ipam=ipam_config
    )

    # now map it as part of the networking configuration
    networking_config = client.create_networking_config({
		'pytest_monitor': client.create_endpoint_config(ipv4_address=container_ip)
	})

    # "create" the container, which really doesn't create an actual image, it
    # basically constructs the object needed to start one. This is a 2-step
    # process (equivalent to 'docker run'). It also uses the
    # `networking_config` and `container_network` created above. These are
    # needed because the requirement for the Ceph containers is to know the IP
    # and the subnet(s) beforehand.
    container = client.create_container(
        image=request.param,
        name='pytest_ceph_mon',
        environment={'CEPH_DAEMON': 'MON', 'MON_IP': container_ip, 'CEPH_PUBLIC_NETWORK': subnet},
        detach=True,
        networking_config=networking_config,
        command='ceph/daemon mon'
    )

    def teardown():
        client.remove_container(
            container=container['Id'],
            force=True
        )
        client.remove_network(container_network['Id'])

    try:
        client.start(container=container["Id"])
    except Exception as err:
        teardown()
        raise
    else:
        # this is non-ideal, we can't tell for sure when the container is really really up
        # after the entry point script has run
        import time;time.sleep(0.5)
        if client.inspect_container(container)['State']['ExitCode'] > 0:
            print "[ERROR][setup] failed to setup container for %s" %  request.param
            for line in client.logs(container, stream=True):
                print "[ERROR][setup]", line.strip('\n')
            raise RuntimeError()

        yield container

    teardown()
