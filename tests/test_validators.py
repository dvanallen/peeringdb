import ipaddress
import os

import pytest
import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import RequestFactory, override_settings

from peeringdb_server.context import current_request
from peeringdb_server.models import (
    Facility,
    InternetExchange,
    IXLan,
    IXLanPrefix,
    Network,
    NetworkContact,
    NetworkIXLan,
    Organization,
    ProtectedAction,
)
from peeringdb_server.validators import (
    validate_address_space,
    validate_info_prefixes4,
    validate_info_prefixes6,
    validate_irr_as_set,
    validate_phonenumber,
    validate_prefix_overlap,
)
from tests.test_ixf_member_import_protocol import setup_test_data

pytestmark = pytest.mark.django_db

INVALID_ADDRESS_SPACES = [
    "0.0.0.0/1",
    "0.0.0.0/8",
    "10.0.0.0/8",
    "127.0.0.0/8",
    "169.254.0.0/16",
    # FIXME: this fails still
    #'172.0.0.0/11',
    "172.16.0.0/12",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    # FIXME: this fails still
    #'224.0.0.0/3',
    "224.0.0.0/4",
    "240.0.0.0/4",
    "100.64.0.0/10",
    "0000::/8",
    "0064:ff9b::/96",
    "0100::/8",
    "0200::/7",
    "0400::/6",
    "0800::/5",
    "1000::/4",
    "2001::/33",
    "2001:0:8000::/33",
    "2001:0002::/48",
    "2001:0003::/32",
    "2001:10::/28",
    "2001:20::/28",
    "2001:db8::/32",
    "2002::/16",
    "3ffe::/16",
    "4000::/2",
    "4000::/3",
    "5f00::/8",
    "6000::/3",
    "8000::/3",
    "a000::/3",
    "c000::/3",
    "e000::/4",
    "f000::/5",
    "f800::/6",
    "fc00::/7",
    "fe80::/10",
    "fec0::/10",
    "ff00::/8",
]


@pytest.fixture(params=INVALID_ADDRESS_SPACES)
def prefix(request):
    return request.param


# @pytest.mark.django_db
def test_validate_address_space(prefix):
    """
    Tests peeringdb_server.validators.validate_address_space
    """
    with pytest.raises(ValidationError) as exc:
        validate_address_space(ipaddress.ip_network(str(prefix)))


@override_settings(DATA_QUALITY_MAX_PREFIX_V4_LIMIT=500000)
def test_validate_info_prefixes4():
    """
    Tests peeringdb_server.validators.validate_info_prefixes4
    """
    with pytest.raises(ValidationError):
        validate_info_prefixes4(500001)
    with pytest.raises(ValidationError):
        validate_info_prefixes4(-1)
    validate_info_prefixes4(500000)
    assert validate_info_prefixes4(None) == 0
    assert validate_info_prefixes4("") == 0


@override_settings(DATA_QUALITY_MAX_PREFIX_V6_LIMIT=500000)
def test_validate_info_prefixes6():
    """
    Tests peeringdb_server.validators.validate_info_prefixes6
    """
    with pytest.raises(ValidationError):
        validate_info_prefixes6(500001)
    with pytest.raises(ValidationError):
        validate_info_prefixes6(-1)
    validate_info_prefixes6(500000)
    assert validate_info_prefixes6(None) == 0
    assert validate_info_prefixes6("") == 0


@override_settings(
    DATA_QUALITY_MIN_PREFIXLEN_V4=24,
    DATA_QUALITY_MAX_PREFIXLEN_V4=24,
    DATA_QUALITY_MIN_PREFIXLEN_V6=48,
    DATA_QUALITY_MAX_PREFIXLEN_V6=48,
)
def test_validate_prefixlen():
    """
    Tests prefix length limits
    """
    with pytest.raises(ValidationError):
        validate_address_space("37.77.32.0/20")
    with pytest.raises(ValidationError):
        validate_address_space("131.72.77.240/28")
    with pytest.raises(ValidationError):
        validate_address_space("2403:c240::/32")
    with pytest.raises(ValidationError):
        validate_address_space("2001:504:0:2::/64")


@pytest.mark.django_db
def test_validate_prefix_overlap():
    org = Organization.objects.create(name="Test org", status="ok")
    ix = InternetExchange.objects.create(name="Text exchange", status="ok", org=org)
    ixlan = ix.ixlan

    pfx1 = IXLanPrefix.objects.create(
        ixlan=ixlan,
        protocol="IPv4",
        prefix=ipaddress.ip_network("198.32.125.0/24"),
        status="ok",
    )

    with pytest.raises(ValidationError) as exc:
        validate_prefix_overlap("198.32.124.0/23")


@pytest.mark.parametrize(
    "value,validated",
    [
        # success validation
        ("RIPE::AS-FOO", "RIPE::AS-FOO"),
        ("AS-FOO@RIPE", "AS-FOO@RIPE"),
        ("AS-FOO-BAR@RIPE", "AS-FOO-BAR@RIPE"),
        ("ripe::as-foo", "RIPE::AS-FOO"),
        ("as-foo@ripe", "AS-FOO@RIPE"),
        ("as-foo@ripe as-bar@ripe", "AS-FOO@RIPE AS-BAR@RIPE"),
        ("as-foo@ripe,as-bar@ripe", "AS-FOO@RIPE AS-BAR@RIPE"),
        ("as-foo@ripe, as-bar@ripe", "AS-FOO@RIPE AS-BAR@RIPE"),
        (
            "RIPE::AS12345:AS-FOO RIPE::AS12345:AS-FOO:AS9876",
            "RIPE::AS12345:AS-FOO RIPE::AS12345:AS-FOO:AS9876",
        ),
        ("ripe::as-foo:as123:as345", "RIPE::AS-FOO:AS123:AS345"),
        ("RIPE::AS12345", "RIPE::AS12345"),
        ("AS12345@RIPE", "AS12345@RIPE"),
        ("RIPE::AS123456:RS-FOO", "RIPE::AS123456:RS-FOO"),
        ("as-foo", "AS-FOO"),
        ("rs-foo", "RS-FOO"),
        ("as-foo as-bar", "AS-FOO AS-BAR"),
        ("rs-foo as-bar", "RS-FOO AS-BAR"),
        ("rs-foo rs-bar", "RS-FOO RS-BAR"),
        ("AS15562", "AS15562"),
        ("AS-15562", "AS-15562"),
        ("AS15562 AS33333", "AS15562 AS33333"),
        # hyphenated source validation
        ("AS-20C@ARIN-NONAUTH", "AS-20C@ARIN-NONAUTH"),
        ("ARIN-NONAUTH::AS-20C", "ARIN-NONAUTH::AS-20C"),
        # fail validation
        ("UNKNOWN::AS-FOO", False),
        ("AS-FOO@UNKNOWN", False),
        ("ASFOO@UNKNOWN", False),
        ("UNKNOWN::ASFOO", False),
        ("RIPE:AS-FOO", False),
        ("RIPE::RS15562:RS-FOO", False),
        ("RIPE::AS123456:RS-FOO:AS-FOO", False),
        ('!"*([])?.=+/\\', False),
        ('RIPE::!"*([])?.=+/\\', False),
        ('!"*([])?.=+/\\@RIPE', False),
        # > DATA_QUALITY_MAX_IRR_DEPTH
        ("ripe::as-foo:as123:as345:as678", False),
    ],
)
def test_validate_irr_as_set(value, validated):
    if not validated:
        with pytest.raises(ValidationError):
            validate_irr_as_set(value)
    else:
        assert validate_irr_as_set(value) == validated


@pytest.mark.django_db
def test_validate_phonenumber():

    # test standalone validator

    validate_phonenumber("+1 206 555 0199")
    validate_phonenumber("012065550199", "US")

    with pytest.raises(ValidationError):
        validate_phonenumber("invalid number")

    with pytest.raises(ValidationError):
        validate_phonenumber("012065550199")

    # test model field validation

    org = Organization.objects.create(name="Test org", status="ok")
    ix = InternetExchange.objects.create(
        name="Text exchange",
        status="ok",
        org=org,
        country="US",
        city="Some city",
        region_continent="North America",
        media="Ethernet",
    )
    net = Network.objects.create(name="Text network", asn=12345, status="ok", org=org)
    poc = NetworkContact.objects.create(network=net, status="ok", role="Abuse")

    # test poc phone validation

    with pytest.raises(ValidationError):
        poc.phone = "invalid"
        poc.full_clean()

    poc.phone = "+1 206 555 0199"
    poc.full_clean()

    # test ix phone validation

    with pytest.raises(ValidationError):
        ix.tech_phone = "invalid"
        ix.full_clean()

    ix.tech_phone = "+1 206 555 0199"
    ix.full_clean()

    with pytest.raises(ValidationError):
        ix.policy_phone = "invalid"
        ix.full_clean()

    ix.policy_phone = "+1 206 555 0199"
    ix.full_clean()


@pytest.mark.django_db
def test_validate_ixpfx_ixlan_status_match():
    org = Organization.objects.create(name="Test org", status="ok")
    ix = InternetExchange.objects.create(
        name="Text exchange", status="pending", org=org
    )
    ixlan = ix.ixlan

    pfx = IXLanPrefix.objects.create(
        ixlan=ixlan,
        protocol="IPv4",
        prefix=ipaddress.ip_network("198.32.125.0/24"),
        status="ok",
    )

    with pytest.raises(ValidationError) as exc1:
        pfx.clean()

    assert (
        exc1.value.args[0]
        == "IXLanPrefix with status 'ok' cannot be linked to a IXLan with status 'pending'."
    )

    ixlan.status = "deleted"
    ixlan.save()
    pfx.status = "pending"
    pfx.save()

    with pytest.raises(ValidationError) as exc2:
        pfx.clean()

    assert (
        exc2.value.args[0]
        == "IXLanPrefix with status 'pending' cannot be linked to a IXLan with status 'deleted'."
    )


@pytest.mark.django_db
@override_settings(
    DATA_QUALITY_MAX_PREFIX_V4_LIMIT=500000,
    DATA_QUALITY_MAX_PREFIX_V6_LIMIT=500000,
    DATA_QUALITY_MIN_PREFIXLEN_V4=24,
    DATA_QUALITY_MAX_PREFIXLEN_V4=24,
    DATA_QUALITY_MIN_PREFIXLEN_V6=48,
    DATA_QUALITY_MAX_PREFIXLEN_V6=48,
    DATA_QUALITY_MAX_IRR_DEPTH=3,
    DATA_QUALITY_MIN_SPEED=10,
    DATA_QUALITY_MAX_SPEED=100,
)
def test_bypass_validation():

    User = get_user_model()

    superuser = User.objects.create_user(
        username="superuser",
        password="superuser",
        email="su@localhost",
        is_superuser=True,
    )
    user = User.objects.create_user(
        username="user", password="user", email="user@localhost"
    )

    factory = RequestFactory()

    org = Organization.objects.create(name="Test org", status="ok")
    ix = InternetExchange.objects.create(
        name="Text exchange",
        status="ok",
        org=org,
        country="US",
        city="Some city",
        region_continent="North America",
        media="Ethernet",
    )
    net = Network.objects.create(name="Text network", asn=12345, status="ok", org=org)

    # super user should bypass validation

    request = factory.get("/")
    request.user = superuser
    with current_request(request):
        validate_address_space("37.77.32.0/20")
        validate_address_space("131.72.77.240/28")
        validate_address_space("2403:c240::/32")
        validate_address_space("2001:504:0:2::/64")
        validate_info_prefixes4(500001)
        validate_info_prefixes6(500001)
        NetworkIXLan(speed=1, network=net, ixlan=ix.ixlan).clean()
        NetworkIXLan(speed=1000, network=net, ixlan=ix.ixlan).clean()
        validate_irr_as_set("ripe::as-foo:as123:as345:as678")

    # user should NOT bypass validation

    request = factory.get("/")
    request.user = user
    with current_request(request):
        with pytest.raises(ValidationError):
            validate_address_space("37.77.32.0/20")
        with pytest.raises(ValidationError):
            validate_address_space("131.72.77.240/28")
        with pytest.raises(ValidationError):
            validate_address_space("2403:c240::/32")
        with pytest.raises(ValidationError):
            validate_address_space("2001:504:0:2::/64")
        with pytest.raises(ValidationError):
            validate_info_prefixes4(500001)
        with pytest.raises(ValidationError):
            validate_info_prefixes6(500001)
        with pytest.raises(ValidationError):
            NetworkIXLan(speed=1, network=net, ixlan=ix.ixlan).clean()
        with pytest.raises(ValidationError):
            NetworkIXLan(speed=1000, network=net, ixlan=ix.ixlan).clean()
        with pytest.raises(ValidationError):
            validate_irr_as_set("ripe::as-foo:as123:as345:as678")


@pytest.mark.django_db
def test_ghost_peer_vs_real_peer_one_netixlan():
    """
    Tests that a real peer can claim the ip addresses of a gohst peer. #983

    In this test both ipv4 and ipv6 exist on the same netixlan.
    """

    # set up entities

    org = Organization.objects.create(name="Test org", status="ok")
    ix = InternetExchange.objects.create(name="Test ix", status="ok", org=org)
    network = Network.objects.create(asn=1001, name="AS1001", status="ok", org=org)
    network_other = Network.objects.create(
        asn=1010, name="AS1010", status="ok", org=org
    )
    ixlan = ix.ixlan
    ixlan.ixf_ixp_member_list_url = "https://localhost/ix-f"
    ixlan.save()

    IXLanPrefix.objects.create(
        ixlan=ixlan,
        status="ok",
        prefix="195.69.144.0/22",
        protocol="IPv4",
    )

    IXLanPrefix.objects.create(
        ixlan=ixlan,
        status="ok",
        prefix="2001:7f8:1::/64",
        protocol="IPv6",
    )

    IP4 = "195.69.147.250"
    IP6 = "2001:7f8:1::a500:2906:1"

    ghost_peer = NetworkIXLan.objects.create(
        network=network_other,
        ixlan=ixlan,
        asn=network_other.asn,
        speed=20000,
        ipaddr4=IP4,
        ipaddr6=IP6,
        status="ok",
        is_rs_peer=False,
        operational=False,
    )

    # setup ix-f cache

    data = setup_test_data("ixf.member.1")
    cache.set(f"IXF-CACHE-{ix.ixlan.ixf_ixp_member_list_url}", data)

    ix = ixlan.ix

    # real peer should exist in ix-f data

    real4, real6 = ix.peer_exists_in_ixf_data(1001, IP4, IP6)
    assert real4
    assert real6

    # ghost peer should NOT exist in ix-f data

    ghost4, ghost6 = ix.peer_exists_in_ixf_data(1010, IP4, IP6)
    assert not ghost4
    assert not ghost6

    # create and save a real peer that has the same ip addresses
    # as the ghost peer

    real_peer = NetworkIXLan(
        network=network,
        status="ok",
        ipaddr4=IP4,
        ipaddr6=IP6,
        ixlan=ixlan,
        speed=1000,
        asn=network.asn,
    )

    # run full validation (this will run `validate_real_vs_ghost_peer`)

    real_peer.full_clean()
    real_peer.save()

    # real peer has been saved and since it claimed both ip4 and ip6, the ghost
    # peer is now deleted

    ghost_peer.refresh_from_db()

    assert ghost_peer.status == "deleted"


@pytest.mark.django_db
def test_ghost_peer_vs_real_peer_two_netixlan():
    """
    Tests that a real peer can claim the ip addresses of a gohst peer. #983

    In this test both ipv4 and ipv6 exist on separate netixlans.

    In this test both conflicting netixlans will have neither ipv4 nor ipv6 set in the end
    and will be deleted
    """

    # set up entities

    org = Organization.objects.create(name="Test org", status="ok")
    ix = InternetExchange.objects.create(name="Test ix", status="ok", org=org)
    network = Network.objects.create(asn=1001, name="AS1001", status="ok", org=org)
    network_other = Network.objects.create(
        asn=1010, name="AS1010", status="ok", org=org
    )
    ixlan = ix.ixlan
    ixlan.ixf_ixp_member_list_url = "https://localhost/ix-f"
    ixlan.save()

    IXLanPrefix.objects.create(
        ixlan=ixlan,
        status="ok",
        prefix="195.69.144.0/22",
        protocol="IPv4",
    )

    IXLanPrefix.objects.create(
        ixlan=ixlan,
        status="ok",
        prefix="2001:7f8:1::/64",
        protocol="IPv6",
    )

    IP4 = "195.69.147.250"
    IP6 = "2001:7f8:1::a500:2906:1"

    ghost_peer_a = NetworkIXLan.objects.create(
        network=network_other,
        ixlan=ixlan,
        asn=network_other.asn,
        speed=20000,
        ipaddr4=IP4,
        ipaddr6=None,
        status="ok",
        is_rs_peer=False,
        operational=False,
    )

    ghost_peer_b = NetworkIXLan.objects.create(
        network=network_other,
        ixlan=ixlan,
        asn=network_other.asn,
        speed=20000,
        ipaddr4=None,
        ipaddr6=IP6,
        status="ok",
        is_rs_peer=False,
        operational=False,
    )

    # setup ix-f data

    data = setup_test_data("ixf.member.1")
    cache.set(f"IXF-CACHE-{ix.ixlan.ixf_ixp_member_list_url}", data)

    ix = ixlan.ix

    # real peer should exist in ix-f data

    real4, real6 = ix.peer_exists_in_ixf_data(1001, IP4, IP6)
    assert real4
    assert real6

    # ghost peer should NOT exist in ix-f data

    ghost4, ghost6 = ix.peer_exists_in_ixf_data(1010, IP4, IP6)
    assert not ghost4
    assert not ghost6

    # create and save a real peer that has the same ip addresses
    # as the ghost peer

    real_peer = NetworkIXLan(
        network=network,
        status="ok",
        ipaddr4=IP4,
        ipaddr6=IP6,
        ixlan=ixlan,
        speed=1000,
        asn=network.asn,
    )

    # run full validation (this will run `validate_real_vs_ghost_peer`)

    real_peer.full_clean()
    real_peer.save()

    # real peer has been saved and since it claimed both ip4 and ip6, the ghost
    # peer is now deleted

    ghost_peer_a.refresh_from_db()
    ghost_peer_b.refresh_from_db()

    assert ghost_peer_a.status == "deleted"
    assert ghost_peer_b.status == "deleted"


@pytest.mark.django_db
def test_ghost_peer_vs_real_peer_two_netixlan_partial():
    """
    Tests that a real peer can claim the ip addresses of a gohst peer. #983

    In this test both ipv4 and ipv6 exist on separate netixlans.

    In this test the conflicting netixlans will have the other ip address still set and will not be deleted.
    """

    # set up entities

    org = Organization.objects.create(name="Test org", status="ok")
    ix = InternetExchange.objects.create(name="Test ix", status="ok", org=org)
    network = Network.objects.create(asn=1001, name="AS1001", status="ok", org=org)
    network_other = Network.objects.create(
        asn=1010, name="AS1010", status="ok", org=org
    )
    ixlan = ix.ixlan
    ixlan.ixf_ixp_member_list_url = "https://localhost/ix-f"
    ixlan.save()

    IXLanPrefix.objects.create(
        ixlan=ixlan,
        status="ok",
        prefix="195.69.144.0/22",
        protocol="IPv4",
    )

    IXLanPrefix.objects.create(
        ixlan=ixlan,
        status="ok",
        prefix="2001:7f8:1::/64",
        protocol="IPv6",
    )

    IP4 = "195.69.147.250"
    IP6 = "2001:7f8:1::a500:2906:1"

    ghost_peer_a = NetworkIXLan.objects.create(
        network=network_other,
        ixlan=ixlan,
        asn=network_other.asn,
        speed=20000,
        ipaddr4=IP4,
        ipaddr6="2001:7f8:1::a500:2906:2",
        status="ok",
        is_rs_peer=False,
        operational=False,
    )

    ghost_peer_b = NetworkIXLan.objects.create(
        network=network_other,
        ixlan=ixlan,
        asn=network_other.asn,
        speed=20000,
        ipaddr4="195.69.147.251",
        ipaddr6=IP6,
        status="ok",
        is_rs_peer=False,
        operational=False,
    )

    # setup ix-f data

    data = setup_test_data("ixf.member.1")
    cache.set(f"IXF-CACHE-{ix.ixlan.ixf_ixp_member_list_url}", data)

    ix = ixlan.ix

    # real peer should exist in ix-f data

    real4, real6 = ix.peer_exists_in_ixf_data(1001, IP4, IP6)
    assert real4
    assert real6

    # ghost peer should NOT exist in ix-f data

    ghost4, ghost6 = ix.peer_exists_in_ixf_data(1010, IP4, IP6)
    assert not ghost4
    assert not ghost6

    # create and save a real peer that has the same ip addresses
    # as the ghost peer

    real_peer = NetworkIXLan(
        network=network,
        status="ok",
        ipaddr4=IP4,
        ipaddr6=IP6,
        ixlan=ixlan,
        speed=1000,
        asn=network.asn,
    )

    # run full validation (this will run `validate_real_vs_ghost_peer`)

    real_peer.full_clean()
    real_peer.save()

    # real peer has been saved and since it only claimed one ip address
    # from either ghost peer, both ghost peers remain

    ghost_peer_a.refresh_from_db()
    ghost_peer_b.refresh_from_db()

    assert ghost_peer_a.status == "ok"
    assert ghost_peer_a.ipaddr4 is None
    assert ghost_peer_a.ipaddr6 is not None

    assert ghost_peer_b.status == "ok"
    assert ghost_peer_b.ipaddr4 is not None
    assert ghost_peer_b.ipaddr6 is None


@pytest.mark.django_db
def test_ghost_peer_vs_real_peer_invalid_ixf_data():
    """
    Tests that a real peer can claim the ip addresses of a gohst peer. #983

    Test the handling of invalid ix-f data, in which case the ghost peer vs real peer
    logic should be skipped.
    """

    # set up entities

    org = Organization.objects.create(name="Test org", status="ok")
    ix = InternetExchange.objects.create(name="Test ix", status="ok", org=org)
    network = Network.objects.create(asn=1001, name="AS1001", status="ok", org=org)
    network_other = Network.objects.create(
        asn=1010, name="AS1010", status="ok", org=org
    )
    ixlan = ix.ixlan
    ixlan.ixf_ixp_member_list_url = "https://localhost/ix-f"
    ixlan.save()

    IXLanPrefix.objects.create(
        ixlan=ixlan,
        status="ok",
        prefix="195.69.144.0/22",
        protocol="IPv4",
    )

    IXLanPrefix.objects.create(
        ixlan=ixlan,
        status="ok",
        prefix="2001:7f8:1::/64",
        protocol="IPv6",
    )

    IP4 = "195.69.147.250"
    IP6 = "2001:7f8:1::a500:2906:1"

    ghost_peer = NetworkIXLan.objects.create(
        network=network_other,
        ixlan=ixlan,
        asn=network_other.asn,
        speed=20000,
        ipaddr4=IP4,
        ipaddr6=IP6,
        status="ok",
        is_rs_peer=False,
        operational=False,
    )
    # setup ix-f data

    cache.set(f"IXF-CACHE-{ix.ixlan.ixf_ixp_member_list_url}", {"invalid": "data"})

    ix = ixlan.ix

    real_peer = NetworkIXLan(
        network=network,
        status="ok",
        ipaddr4=IP4,
        ipaddr6=IP6,
        ixlan=ixlan,
        speed=1000,
        asn=network.asn,
    )

    # run full validation (this will run `validate_real_vs_ghost_peer`)

    with pytest.raises(Exception) as excinfo:
        real_peer.full_clean()
    assert "IP already exists" in str(excinfo.value)
