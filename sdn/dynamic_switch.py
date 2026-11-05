"""
dynamic_switch.py - learning switch that reacts to link changes (Delivery 2).

This replaces forwarding.l2_learning when the network has more than one switch.
It forwards exactly like l2_learning (learn the source port, flood unknown
destinations, install an exact-match flow), but it can also react when the
topology changes.

Modes (--mode=...):
    dynamic   (default) react to topology changes:
                - a switch port going down or up is noticed straight away
                  (OpenFlow port-status), and discovery is told to drop the
                  links on that port instead of waiting for its link timeout;
                - spanning tree is told to recompute once discovery's link table
                  is up to date (stock POX recomputes it too early and can leave
                  a switch cut off after a failure);
                - whenever a link is added or removed, every switch's flow
                  table is cleared and every learned MAC table is emptied,
                  so traffic is relearned over the new spanning tree.
    static    never reacts. This is the same learning switch as l2_learning
              (stale flows survive until they time out). It is the baseline
              for the performance comparison.

Run it together with discovery and spanning tree (see sdn/README.md):
    cd ~/pox
    PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py openflow.discovery \\
        openflow.spanning_tree dynamic_switch --mode=dynamic job_status

Every reaction is logged with a Unix timestamp ("t=1730000000.123"), so a
script can compute recovery time from the POX log.
"""
import time

from pox.core import core
import pox.openflow.libopenflow_01 as of
from pox.lib.util import dpid_to_str

log = core.getLogger()

MODES = ("dynamic", "static")
IDLE_TIMEOUT = 10      # same as l2_learning
HARD_TIMEOUT = 30      # same as l2_learning
FLUSH_BATCH_DELAY = 0.2    # seconds: gather the events of one change into one flush
FLUSH_AGAIN_DELAY = 1.5    # seconds: flush again after spanning tree has moved its ports


class SwitchBrain(object):
    """The learning logic for one OpenFlow switch (a copy of l2_learning's)."""

    def __init__(self, connection):
        self.connection = connection
        self.macToPort = {}
        connection.addListeners(self)

    def flush(self):
        """Forget everything learned and delete every flow on this switch."""
        self.macToPort.clear()
        self.connection.send(of.ofp_flow_mod(command=of.OFPFC_DELETE))
        # Deleting all flows also removes the flow discovery uses to catch
        # LLDP packets, so put it back.
        discovery = getattr(core, "openflow_discovery", None)
        if discovery is not None:
            discovery.install_flow(self.connection)

    def _handle_PacketIn(self, event):
        packet = event.parsed
        if not packet.parsed:
            return

        def flood():
            msg = of.ofp_packet_out()
            msg.actions.append(of.ofp_action_output(port=of.OFPP_FLOOD))
            msg.data = event.ofp
            msg.in_port = event.port
            self.connection.send(msg)

        def drop(duration=None):
            if duration is not None:
                msg = of.ofp_flow_mod()
                msg.match = of.ofp_match.from_packet(packet)
                msg.idle_timeout = duration
                msg.hard_timeout = duration
                msg.buffer_id = event.ofp.buffer_id
                self.connection.send(msg)
            elif event.ofp.buffer_id is not None:
                msg = of.ofp_packet_out()
                msg.buffer_id = event.ofp.buffer_id
                msg.in_port = event.port
                self.connection.send(msg)

        self.macToPort[packet.src] = event.port

        # Never forward link-local traffic (LLDP, bridge-filtered addresses)
        if packet.type == packet.LLDP_TYPE or packet.dst.isBridgeFiltered():
            drop()
            return

        if packet.dst.is_multicast or packet.dst not in self.macToPort:
            flood()
            return

        port = self.macToPort[packet.dst]
        if port == event.port:
            log.warning("Same port for packet from %s -> %s on %s.%s. Drop.",
                        packet.src, packet.dst, dpid_to_str(event.dpid), port)
            drop(10)
            return

        msg = of.ofp_flow_mod()
        msg.match = of.ofp_match.from_packet(packet, event.port)
        msg.idle_timeout = IDLE_TIMEOUT
        msg.hard_timeout = HARD_TIMEOUT
        msg.actions.append(of.ofp_action_output(port=port))
        msg.data = event.ofp
        self.connection.send(msg)


class DynamicSwitch(object):
    """Creates a SwitchBrain per switch and handles the topology reactions."""

    def __init__(self, mode):
        self.mode = mode
        self.brains = {}        # dpid -> SwitchBrain
        self.link_up = {}       # (dpid, port_no) -> bool, last known link state
        self._flush_pending = False
        self.flushes = 0        # how many flushes were done (for tests and logs)

        core.openflow.addListeners(self)
        if mode == "dynamic":
            discovery = getattr(core, "openflow_discovery", None)
            if discovery is None:
                raise RuntimeError("dynamic_switch needs openflow.discovery "
                                   "(put it before dynamic_switch on the command line)")
            discovery.addListenerByName("LinkEvent", self._handle_LinkEvent)
        log.info("Dynamic switch started in %s mode", mode)

    # ---- switches coming and going ----

    def _handle_ConnectionUp(self, event):
        self.brains[event.dpid] = SwitchBrain(event.connection)
        for port in event.connection.ports.values():
            if port.port_no < of.OFPP_MAX:
                self.link_up[(event.dpid, port.port_no)] = not (port.state & of.OFPPS_LINK_DOWN)

    def _handle_ConnectionDown(self, event):
        self.brains.pop(event.dpid, None)
        for key in [k for k in self.link_up if k[0] == event.dpid]:
            del self.link_up[key]

    # ---- reactions (dynamic mode only) ----

    def _handle_PortStatus(self, event):
        if self.mode != "dynamic":
            return
        port_no = event.port
        if port_no >= of.OFPP_MAX:
            return
        up = not (event.ofp.desc.state & of.OFPPS_LINK_DOWN)
        key = (event.dpid, port_no)
        before = self.link_up.get(key)
        self.link_up[key] = up
        if before is None or before == up:
            return      # first time we hear of this port, or only its config changed
        log.info("t=%.3f port %s.%d went %s", time.time(),
                 dpid_to_str(event.dpid), port_no, "up" if up else "down")
        if not up:
            self._drop_links_on_port(event.dpid, port_no)
        self.request_flush("port %s.%d %s" % (dpid_to_str(event.dpid), port_no,
                                              "up" if up else "down"))

    def _refresh_tree(self):
        """Make spanning tree recompute now, from discovery's current links.

        Discovery raises its link-removed event BEFORE it deletes the link from
        its own table, so spanning tree recomputes with the dead link still in
        and keeps (or builds) a wrong tree, and nothing triggers it again. That
        can leave a switch cut off, with the only link to it blocked. Asking for
        a recompute once the table is up to date fixes it."""
        try:
            import pox.openflow.spanning_tree as spanning_tree
            if hasattr(core, "openflow_discovery"):
                spanning_tree._update_tree()
        except Exception:
            log.exception("Could not refresh the spanning tree")

    def _drop_links_on_port(self, dpid, port_no):
        """Tell discovery the links on this port are gone, right now, instead of
        waiting for its link timeout. (Uses discovery's own method for this.)"""
        discovery = getattr(core, "openflow_discovery", None)
        if discovery is None:
            return
        try:
            dead = [link for link in list(discovery.adjacency)
                    if (link.dpid1 == dpid and link.port1 == port_no)
                    or (link.dpid2 == dpid and link.port2 == port_no)]
            if dead:
                log.info("t=%.3f dropping %d link(s) on port %s.%d without waiting for the timeout",
                         time.time(), len(dead), dpid_to_str(dpid), port_no)
                discovery._delete_links(dead)
                self._refresh_tree()
        except Exception:
            log.exception("Could not drop links early; discovery will time them out instead")

    def _handle_LinkEvent(self, event):
        self.request_flush("link %s %s" % (
            "added" if event.added else "removed", event.link))

    def request_flush(self, reason):
        """Flush soon, and once more a moment later. Events that arrive together
        (both directions of a link, two ends of a port) share one flush."""
        if self._flush_pending:
            return
        self._flush_pending = True
        log.info("t=%.3f flush requested: %s", time.time(), reason)
        core.call_delayed(FLUSH_BATCH_DELAY, self._flush_all, reason)
        core.call_delayed(FLUSH_BATCH_DELAY + FLUSH_AGAIN_DELAY, self._flush_all,
                          reason + " (second pass)", True)

    def _flush_all(self, reason, second=False):
        if second:
            self._flush_pending = False
        self._refresh_tree()
        self.flushes += 1
        for brain in list(self.brains.values()):
            brain.flush()
        log.info("t=%.3f flushed %d switches: %s", time.time(), len(self.brains), reason)


def launch(mode="dynamic"):
    """POX entry point. Option: --mode=dynamic (default) or --mode=static"""
    if mode not in MODES:
        raise RuntimeError("--mode must be one of: %s" % ", ".join(MODES))
    core.registerNew(DynamicSwitch, mode)
