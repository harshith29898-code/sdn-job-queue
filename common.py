# common.py

# NOTE: The IPS_TABLE below represents a legacy static IP configuration.
# It is retained for backward compatibility reference and is not actively used 
# by the dynamic switch or current runtime topology.
IPS_TABLE = {
    "client1": "10.0.0.1",
    "client2": "10.0.0.2",
    "client3": "10.0.0.3",
    "worker1": "10.0.0.4",
    "worker2": "10.0.0.5",
    "worker3": "10.0.0.6",
}

DEFAULT_SERVER_PORT = 9000
DEFAULT_HEARTBEAT_INTERVAL = 2.0
