#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Stage 3: delay-propagation stress test.

This is the old disease_spread.py SIR model with the labels changed. The math
is identical; only the story is different:

    Susceptible -> Normal Operations
    Infected    -> Delayed
    Recovered   -> Recovered Operations

The scenario: the busiest hub goes down (weather, systems outage, whatever) and
the delay spreads to connected facilities. beta is how likely a delay jumps
along a lane each step; gamma is how likely a delayed facility recovers.

    python src/03_delay_simulation.py
"""

import os
import random
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import networkx as nx

# Data and output/ are one level up from src/.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = PROJECT_ROOT
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")


def _data_path(name):
    return os.path.join(DATA_DIR, name)


def _output_path(name):
    return os.path.join(OUTPUT_DIR, name)


# --- data loading / graph build (same as stage 2) -----------------------

def load_facilities(file_path=None):
    """Read airports.dat and drop rows with no coordinates or no IATA code."""
    file_path = file_path or _data_path("airports.dat")
    cols = ['Airport ID', 'Name', 'City', 'Country', 'IATA', 'ICAO', 'Latitude',
            'Longitude', 'Altitude', 'Timezone', 'DST', 'Tz database time zone',
            'Type', 'Source']

    facilities = pd.read_csv(file_path, header=None, names=cols)
    facilities = facilities[~facilities['Latitude'].isna() & ~facilities['Longitude'].isna()]
    facilities = facilities[facilities['IATA'] != '\\N']
    facilities = facilities[facilities['Latitude'] != '\\N']
    facilities = facilities[facilities['Longitude'] != '\\N']
    return facilities


def load_lanes(file_path=None):
    """Read routes.dat."""
    file_path = file_path or _data_path("routes.dat")
    cols = ['Carrier', 'Carrier ID', 'Origin facility', 'Origin facility ID',
            'Destination facility', 'Destination facility ID', 'Codeshare',
            'Stops', 'Equipment']
    lanes = pd.read_csv(file_path, header=None, names=cols)
    return lanes


def create_operations_network(facilities_df, lanes_df):
    """Directed graph; edge weight = number of routes on the pair."""
    active_facilities = (set(lanes_df['Origin facility'].unique())
                         | set(lanes_df['Destination facility'].unique()))
    facilities_df = facilities_df[facilities_df['IATA'].isin(active_facilities)]

    G = nx.DiGraph()

    facility_data = {}
    for _, row in facilities_df.iterrows():
        iata = row['IATA']
        facility_data[iata] = {
            'name': row['Name'],
            'city': row['City'],
            'country': row['Country'],
            'latitude': row['Latitude'],
            'longitude': row['Longitude'],
        }

    for iata, data in facility_data.items():
        G.add_node(iata, **data)

    lane_counts = {}
    for _, row in lanes_df.iterrows():
        source = row['Origin facility']
        dest = row['Destination facility']
        if source in G and dest in G:
            key = (source, dest)
            lane_counts[key] = lane_counts.get(key, 0) + 1

    for (source, dest), count in lane_counts.items():
        G.add_edge(source, dest, weight=count)

    return G


# --- the model -----------------------------------------------------------
# States: 'N' normal, 'D' delayed, 'R' recovered. (SIR's S/I/R renamed.)

def initialize_states(G, initially_delayed=None):
    """Everyone starts normal except the facilities we seed as delayed."""
    states = {node: 'N' for node in G.nodes()}

    if initially_delayed:
        for facility in initially_delayed:
            if facility in states:
                states[facility] = 'D'
    return states


def step_delay_model(G, states, beta=0.02, gamma=0.01):
    """One timestep. Recover delayed facilities, then spread delays to normal ones.

    Recovery is checked before spread, and we write into a copy so that every
    facility is judged against the state at the *start* of the step (otherwise a
    facility could get delayed and recover in the same tick).
    """
    new_states = states.copy()

    # Delayed -> Recovered, each with probability gamma.
    for node in G.nodes():
        if states[node] == 'D':
            if random.random() < gamma:
                new_states[node] = 'R'

    # Normal -> Delayed. With k delayed neighbours, the chance of staying clean
    # is (1-beta)^k, so the chance of catching a delay is 1 minus that.
    for node in G.nodes():
        if states[node] == 'N':
            delayed_neighbors = 0
            for neighbor in G.neighbors(node):
                if states[neighbor] == 'D':
                    delayed_neighbors += 1
            if delayed_neighbors > 0:
                p_delay = 1.0 - (1.0 - beta) ** delayed_neighbors
                if random.random() < p_delay:
                    new_states[node] = 'D'

    return new_states


def simulate_delay_propagation(G, initially_delayed, steps=50, beta=0.02, gamma=0.01):
    """Run the sim and return per-step counts of (normal, delayed, recovered)."""
    states = initialize_states(G, initially_delayed)

    normal_ops = [sum(s == 'N' for s in states.values())]
    delayed = [sum(s == 'D' for s in states.values())]
    recovered_ops = [sum(s == 'R' for s in states.values())]

    for t in range(1, steps + 1):
        states = step_delay_model(G, states, beta=beta, gamma=gamma)
        normal_ops.append(sum(s == 'N' for s in states.values()))
        delayed.append(sum(s == 'D' for s in states.values()))
        recovered_ops.append(sum(s == 'R' for s in states.values()))

    return normal_ops, delayed, recovered_ops


def plot_delay_curves(normal_ops, delayed, recovered_ops,
                      title="Operational Delay Propagation Stress Test",
                      out_filename=None):
    """The three curves over time - the headline chart for this stage."""
    out_filename = out_filename or _output_path("delay_propagation_stress_test.png")
    os.makedirs(os.path.dirname(out_filename), exist_ok=True)

    plt.figure(figsize=(9, 5.5))
    plt.plot(normal_ops, label='Normal Operations', color='green')
    plt.plot(delayed, label='Delayed', color='red')
    plt.plot(recovered_ops, label='Recovered Operations', color='blue')
    plt.xlabel("Operational Time Step")
    plt.ylabel("Number of Facilities")
    plt.title(title)
    plt.legend()
    plt.grid(True, linestyle='--', alpha=0.7)
    plt.tight_layout()

    plt.savefig(out_filename, dpi=300)
    plt.close()
    print(f"Stress-test plot saved to {out_filename}")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Loading facilities and lanes...")
    facilities_df = load_facilities()
    lanes_df = load_lanes()
    G = create_operations_network(facilities_df, lanes_df)
    print(f"Built an operations network of {len(G.nodes())} facilities "
          f"and {len(G.edges())} lanes.")

    if len(G.nodes()) == 0:
        print("No facilities in network; aborting stress test.")
        return

    # Start the disruption at the busiest facility - worst realistic case.
    major_hub = max(G.degree(), key=lambda x: x[1])[0]
    hub_name = G.nodes[major_hub].get('name', major_hub)
    hub_city = G.nodes[major_hub].get('city', 'Unknown')
    initially_delayed = [major_hub]

    # beta > gamma here on purpose: delays spread faster than they clear, which
    # is what makes a single hub outage cascade.
    steps = 30
    beta = 0.04
    gamma = 0.01

    print(f"\nSimulating a massive disruption at major hub "
          f"{major_hub} ({hub_name}, {hub_city}).")
    print(f"Parameters: steps={steps}, propagation_rate(beta)={beta}, "
          f"recovery_rate(gamma)={gamma}")

    normal_ops, delayed, recovered_ops = simulate_delay_propagation(
        G, initially_delayed, steps=steps, beta=beta, gamma=gamma)

    print(f"\nAt operational step {steps}:")
    print(f"  Normal Operations:    {normal_ops[-1]}")
    print(f"  Delayed:              {delayed[-1]}")
    print(f"  Recovered Operations: {recovered_ops[-1]}")
    print(f"  Peak simultaneous delay: {max(delayed)} facilities")

    plot_delay_curves(
        normal_ops, delayed, recovered_ops,
        title=(f"Delay Propagation Stress Test — Disruption at {major_hub} "
               f"({hub_city})\n(propagation β={beta}, recovery γ={gamma})"))


if __name__ == "__main__":
    main()
