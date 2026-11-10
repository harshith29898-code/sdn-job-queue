# SDN Job Queue

A high-performance Software-Defined Networking (SDN) job queue system with dynamic load scheduling, priority aging, dynamic switch routing, and fault-tolerant worker management.

## Installation & Requirements

Python 3.8+ is required.

While core queuing and network routing run using standard Python libraries, plotting performance graphs and starting the web monitoring GUI require additional packages:

```bash
pip install matplotlib pandas flask
```

---

## Usage Guide

### 1. Load Generator
Generate synthetic job workloads using `app/load_generator.py`:
```bash
python app/load_generator.py --rate 10 --duration 60 --seed 42
```

### 2. Scheduler & Aging Flags
Start the queue server with your preferred scheduling policy and aging rate:
```bash
# FCFS Scheduler
python app/server.py --scheduler fcfs

# Shortest Job First (SJF)
python app/server.py --scheduler sjf

# Priority Scheduling with Aging (Aging rate = 0.5)
python app/server.py --scheduler priority --aging-rate 0.5
```

### 3. Monitoring GUI
To launch the real-time web monitoring dashboard:
```bash
python app/server.py --gui
```
Navigate to `http://localhost:5000` in your web browser to monitor job queues, worker status, and network link metrics.

---

## Running Tests & Experiments

```bash
# Run D1 & D2 unit tests
pytest tests/

# Run experimental benchmark suite and generate graphs
python experiments/local_experiments.py --all
python experiments/analyze_local.py
```
