from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet
import time

THRESHOLD = 20       # packets
TIME_WINDOW = 5      # seconds

class DynamicBlock(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(DynamicBlock, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.packet_log = {}      # {mac: [(timestamp), ...]}
        self.blocked_hosts = set()

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER)]
        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, datapath, priority, match, actions):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath,
                               priority=priority,
                               match=match,
                               instructions=inst)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        src = eth.src
        dst = eth.dst
        dpid = datapath.id

        self.mac_to_port.setdefault(dpid, {})
        in_port = msg.match['in_port']
        self.mac_to_port[dpid][src] = in_port

        #  Track packet timestamps
        now = time.time()
        self.packet_log.setdefault(src, [])
        self.packet_log[src].append(now)

        # Remove old timestamps
        self.packet_log[src] = [
            t for t in self.packet_log[src]
            if now - t <= TIME_WINDOW
        ]

        #  Dynamic Blocking Condition
        if len(self.packet_log[src]) > THRESHOLD and src not in self.blocked_hosts:
            self.logger.info(f"Blocking host {src}")

            match = parser.OFPMatch(eth_src=src)
            actions = []  # DROP

            self.add_flow(datapath, 10, match, actions)
            self.blocked_hosts.add(src)
            return

        # Normal forwarding (learning switch)
        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        match = parser.OFPMatch(in_port=in_port, eth_dst=dst)
        self.add_flow(datapath, 1, match, actions)

        out = parser.OFPPacketOut(datapath=datapath,
                                 buffer_id=msg.buffer_id,
                                 in_port=in_port,
                                 actions=actions,
                                 data=msg.data)

        datapath.send_msg(out)
