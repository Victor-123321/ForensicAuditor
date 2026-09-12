import networkx as nx

from graph.detectors import (
    detect_blacklist_matches,
    detect_cycles,
    detect_shared_attribute_clusters,
)


def test_detect_cycles_finds_round_trip():
    g = nx.MultiDiGraph()
    g.add_edge("acc-A", "acc-B", type="EXECUTED_PAYMENT", amount=100)
    g.add_edge("acc-B", "acc-C", type="EXECUTED_PAYMENT", amount=100)
    g.add_edge("acc-C", "acc-A", type="EXECUTED_PAYMENT", amount=100)

    leads = detect_cycles(g)
    assert len(leads) == 1
    assert set(leads[0].entity_ids) == {"acc-A", "acc-B", "acc-C"}


def test_detect_blacklist_matches():
    g = nx.MultiDiGraph()
    g.add_node("RFC1", type="Company", blacklist_status="definitivo")
    g.add_node("RFC2", type="Company", blacklist_status="none")

    leads = detect_blacklist_matches(g)
    assert len(leads) == 1
    assert leads[0].entity_ids == ["RFC1"]


def test_shared_attribute_cluster_needs_at_least_two():
    g = nx.MultiDiGraph()
    g.add_edge("RFC1", "RFC2", type="SHARES_ADDRESS")

    leads = detect_shared_attribute_clusters(g)
    assert len(leads) == 1
    assert set(leads[0].entity_ids) == {"RFC1", "RFC2"}


def test_no_false_positives_on_clean_graph():
    g = nx.MultiDiGraph()
    g.add_node("RFC1", type="Company", blacklist_status="none")
    g.add_node("RFC2", type="Company", blacklist_status="none")
    g.add_edge("acc-RFC1", "acc-RFC2", type="EXECUTED_PAYMENT", amount=100, reference="inv-1")
    g.add_node("inv-1", type="Invoice", amount=100)

    assert detect_blacklist_matches(g) == []
    assert detect_cycles(g) == []
    assert detect_shared_attribute_clusters(g) == []
