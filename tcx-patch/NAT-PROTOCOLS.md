# NAT non-port protocol support

Landscape NAT4 previously translated only TCP, UDP and ICMP. It now also
handles the port-less protocols a typical home router passes through:

| Protocol | Number | Notes |
|---|---:|---|
| IPIP | 4 | outer IPv4 only |
| GRE | 47 | PPTP/GRE; GRE checksum does not cover the outer IPv4 header |
| ESP | 50 | IPsec transport/tunnel; ESP integrity does not cover outer IPv4 |
| L2TP | 115 | raw L2TP over IP |

## Data path

Both the TC and XDP NAT paths use a per-protocol mapping with port `0`:

- outbound: source address is replaced with the WAN address
- inbound: destination is mapped back to the single internal host that owns
  that protocol
- no L4 checksum is touched; only the IPv4 header checksum and addresses

## Limitations

- One internal host per protocol. Multiple PPTP/IPsec clients behind the same
  WAN address need a protocol-aware ALG or extra WAN addresses.
- AH (51) is not translated: its ICV covers the outer IPv4 addresses, so any
  address rewrite invalidates the packet.
- SCTP/DCCP/AH are not included; SCTP and DCCP would need their own checksum
  handling.
- Static port-forward entries remain TCP/UDP only; GRE/ESP work as outbound
  passthrough.
