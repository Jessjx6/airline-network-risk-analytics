#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Stage 2 of the pipeline: build the operations network and score it.

This started life as the academic flight_network.py and keeps the same graph
math, just under operations names. Quick translation if you're reading the old
code alongside this:

    airports / nodes  -> facilities
    routes / edges    -> lanes (edge weight = how many routes on the pair)
    community detection -> regional hub optimization
    centrality        -> Network Vulnerability Index
    hub-removal test  -> disruption resilience stress test

Loads the data, builds the graph, scores each facility, clusters the regional
hubs, runs the stress test, and writes output/facility_metrics.csv for Tableau.

    python src/02_network_analytics.py
"""

import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import networkx as nx

# cartopy is only needed for the world map; everything else works without it.
try:
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    _HAS_CARTOPY = True
except Exception:
    _HAS_CARTOPY = False

import community as community_louvain

# We're in src/ but the data and output/ live one level up, so anchor paths to
# the project root instead of the current working directory.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = PROJECT_ROOT
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")


def _data_path(name):
    return os.path.join(DATA_DIR, name)


def _output_path(name):
    return os.path.join(OUTPUT_DIR, name)


def load_facilities(file_path=None):
    """Read airports.dat and drop the rows we can't use."""
    file_path = file_path or _data_path("airports.dat")
    cols = ['Airport ID', 'Name', 'City', 'Country', 'IATA', 'ICAO', 'Latitude',
            'Longitude', 'Altitude', 'Timezone', 'DST', 'Tz database time zone',
            'Type', 'Source']

    facilities = pd.read_csv(file_path, header=None, names=cols)
    # No coordinates or no IATA code -> useless to us.
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
    """Build the directed graph. Edge weight = number of routes on the pair."""
    active_facilities = (set(lanes_df['Origin facility'].unique())
                         | set(lanes_df['Destination facility'].unique()))

    # Skip facilities that never appear in a route - they'd be isolated nodes.
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

    # Count routes per (origin, dest); guard against codes we filtered out above.
    lane_counts = {}
    for _, row in lanes_df.iterrows():
        source = row['Origin facility']
        dest = row['Destination facility']
        if source in G.nodes and dest in G.nodes:
            key = (source, dest)
            lane_counts[key] = lane_counts.get(key, 0) + 1

    for (source, dest), count in lane_counts.items():
        G.add_edge(source, dest, weight=count)

    return G


def calculate_network_statistics(G):
    """Headline numbers about the network: size, density, connectivity."""
    stats = {}
    stats['num_nodes'] = len(G.nodes())
    stats['num_edges'] = len(G.edges())
    stats['avg_degree'] = sum(dict(G.degree()).values()) / stats['num_nodes']
    stats['network_density'] = nx.density(G)

    degrees = [d for _, d in G.degree()]
    stats['max_degree'] = max(degrees)
    stats['min_degree'] = min(degrees)
    stats['median_degree'] = np.median(degrees)

    stats['num_weakly_connected'] = nx.number_weakly_connected_components(G)
    largest_wcc = max(nx.weakly_connected_components(G), key=len)
    stats['largest_wcc_size'] = len(largest_wcc)
    stats['largest_wcc_percentage'] = stats['largest_wcc_size'] / stats['num_nodes'] * 100

    # Clustering is only defined on an undirected graph.
    G_undirected = G.to_undirected()
    stats['avg_clustering'] = nx.average_clustering(G_undirected)

    # Path length / diameter can blow up on big graphs, so don't let them
    # take the whole run down with them.
    subgraph = G.subgraph(largest_wcc)
    try:
        stats['avg_path_length'] = nx.average_shortest_path_length(subgraph)
    except Exception:
        stats['avg_path_length'] = "Too large to compute"

    try:
        if nx.is_weakly_connected(subgraph):
            stats['diameter'] = nx.diameter(subgraph)
        else:
            stats['diameter'] = "Network is not connected"
    except Exception:
        stats['diameter'] = "Too large to compute"

    return stats


def plot_throughput_distribution(G, filename=None):
    """Histogram of how many lanes each facility connects to."""
    filename = filename or _output_path("throughput_distribution.png")
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    degrees = [G.degree(n) for n in G.nodes()]

    plt.figure(figsize=(8, 6))
    plt.hist(degrees, bins=range(1, max(degrees) + 2), edgecolor='black', alpha=0.7)
    plt.title("Facility Throughput Distribution (Total Lane Connections)")
    plt.xlabel("Lane Connections per Facility")
    plt.ylabel("Number of Facilities")
    plt.tight_layout()
    plt.savefig(filename, dpi=300, bbox_inches='tight')
    plt.close()
    print(f"Throughput distribution figure saved to {filename}")


def calculate_network_vulnerability_index(G):
    """Three centrality measures, renamed for the ops framing.

    throughput  = degree        (how many lanes touch the facility)
    chokepoint  = betweenness    (how often it sits on shortest paths)
    influence   = eigenvector    (how well-connected its neighbours are)
    """
    throughput_exposure = dict(G.degree())
    chokepoint_exposure = nx.betweenness_centrality(G)

    # Eigenvector centrality doesn't always converge; fall back to degree
    # centrality rather than crashing the run.
    try:
        influence_exposure = nx.eigenvector_centrality(G, max_iter=3000)
    except Exception:
        influence_exposure = nx.degree_centrality(G)
        print("Warning: influence exposure (eigenvector) did not converge; "
              "falling back to normalized throughput.")

    return throughput_exposure, chokepoint_exposure, influence_exposure


def get_top_facilities(score_dict, n=10):
    """Top n facilities for whichever score dict you pass in."""
    return sorted(score_dict.items(), key=lambda x: x[1], reverse=True)[:n]


def analyze_disruption_resilience(G, top_n=20):
    """Pull the busiest facilities one at a time and watch the network split.

    Removing the top hubs in order is a worst-case ("targeted attack") view of
    how fast the network fragments, vs. losing random facilities.
    """
    original_size = len(G)

    facility_throughput = dict(G.degree())
    top_hubs = sorted(facility_throughput.items(), key=lambda x: x[1], reverse=True)[:top_n]

    results = []
    G_copy = G.copy()  # work on a copy so the caller's graph stays intact

    for i, (hub, throughput) in enumerate(top_hubs):
        hub_info = {
            'rank': i + 1,
            'hub': hub,
            'throughput': throughput,
            'name': G.nodes[hub].get('name', 'Unknown'),
            'city': G.nodes[hub].get('city', 'Unknown'),
            'country': G.nodes[hub].get('country', 'Unknown'),
        }

        G_copy.remove_node(hub)

        components = list(nx.weakly_connected_components(G_copy))
        if components:
            largest_cc = max(components, key=len)
            hub_info['nodes_remaining'] = len(G_copy)
            hub_info['largest_cc_size'] = len(largest_cc)
            # Measure against the ORIGINAL size so the % is comparable across steps.
            hub_info['largest_cc_percentage'] = len(largest_cc) / original_size * 100
            hub_info['num_components'] = len(components)
        else:
            hub_info['nodes_remaining'] = 0
            hub_info['largest_cc_size'] = 0
            hub_info['largest_cc_percentage'] = 0
            hub_info['num_components'] = 0

        results.append(hub_info)
        print(f"Stress-tested facility {i + 1}/{top_n}: {hub}")

    return pd.DataFrame(results)


def visualize_disruption_resilience(results_df, filename=None):
    """Two trend charts plus a table of the first 10 hubs removed."""
    filename = filename or _output_path("disruption_resilience.png")
    os.makedirs(os.path.dirname(filename), exist_ok=True)

    plt.figure(figsize=(15, 10))
    gs = gridspec.GridSpec(2, 2)

    ax1 = plt.subplot(gs[0, 0])
    ax1.plot(range(1, len(results_df) + 1), results_df['largest_cc_percentage'],
             'o-', color='blue', linewidth=2)
    ax1.set_xlabel('Facilities Knocked Out', fontsize=10)
    ax1.set_ylabel('Largest Operational Cluster (%)', fontsize=10)
    ax1.set_title('Network Fragmentation Under Facility Loss', fontsize=12)
    ax1.grid(True, linestyle='--', alpha=0.7)

    ax2 = plt.subplot(gs[0, 1])
    ax2.plot(range(1, len(results_df) + 1), results_df['num_components'],
             'o-', color='green', linewidth=2)
    ax2.set_xlabel('Facilities Knocked Out', fontsize=10)
    ax2.set_ylabel('Disconnected Operational Clusters', fontsize=10)
    ax2.set_title('Operational Fragmentation', fontsize=12)
    ax2.grid(True, linestyle='--', alpha=0.7)

    ax3 = plt.subplot(gs[1, :])
    table_data = []
    for i, row in results_df.head(10).iterrows():
        hub_info = f"{row['hub']} ({row['city']}, {row['country']})"
        table_data.append([
            f"{i + 1}",
            hub_info,
            f"{row['throughput']}",
            f"{row['largest_cc_percentage']:.1f}%",
            f"{row['num_components']}",
        ])

    table = ax3.table(
        cellText=table_data,
        colLabels=["Rank", "Facility", "Throughput", "Largest Cluster", "Clusters"],
        loc='center', cellLoc='center',
        colWidths=[0.05, 0.4, 0.12, 0.15, 0.1])
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1.2, 1.5)
    ax3.set_title('Impact of Facility Loss on Network Structure', fontsize=12)
    ax3.axis('off')

    plt.tight_layout()
    plt.savefig(filename, dpi=300, bbox_inches='tight')
    plt.close()
    print(f"Disruption resilience visualization saved to {filename}")


def optimize_regional_hubs(G):
    """Louvain clustering -> {facility: hub_id}.

    Louvain needs an undirected graph, so we collapse direction first. The
    clusters end up grouping facilities that mostly fly among themselves, which
    reads naturally as regional hubs.
    """
    print("Running Regional Hub Optimization...")
    G_undirected = G.to_undirected()
    partition = community_louvain.best_partition(G_undirected)
    return partition


def visualize_regional_hubs(G, partition, filename=None):
    """World map coloured by regional hub (top 10 hubs; rest greyed out)."""
    filename = filename or _output_path("regional_hub_optimization.png")
    os.makedirs(os.path.dirname(filename), exist_ok=True)

    if not _HAS_CARTOPY:
        print("cartopy not available — skipping regional hub geo-map.")
        return

    region_sizes = {}
    for facility, region in partition.items():
        region_sizes[region] = region_sizes.get(region, 0) + 1

    top_regions = sorted(region_sizes.items(), key=lambda x: x[1], reverse=True)[:10]

    fig, ax = _create_world_map_base(
        title="Regional Hub Optimization (Louvain Clustering)")

    colors = plt.cm.tab10(np.linspace(0, 1, len(top_regions)))
    region_colors = {region: colors[i] for i, (region, _) in enumerate(top_regions)}
    top_region_ids = [r for r, _ in top_regions]

    # Everything outside the top 10 hubs is faint grey background.
    for facility in G.nodes():
        if partition.get(facility) not in top_region_ids:
            lon = G.nodes[facility]['longitude']
            lat = G.nodes[facility]['latitude']
            ax.plot(lon, lat, 'o', transform=ccrs.PlateCarree(),
                    markersize=0.5, color='gray', alpha=0.2)

    for region, size in top_regions:
        members = [f for f in G.nodes() if partition.get(f) == region]
        for facility in members:
            lon = G.nodes[facility]['longitude']
            lat = G.nodes[facility]['latitude']
            node_size = 0.5 + 0.3 * np.log1p(G.degree(facility))
            ax.plot(lon, lat, 'o', transform=ccrs.PlateCarree(),
                    markersize=node_size, color=region_colors[region], alpha=0.7)

    # Label each hub by the country of its busiest facility - good enough as a
    # human-readable name for the cluster.
    legend_elements = []
    for i, (region, size) in enumerate(top_regions):
        members = [f for f in G.nodes() if partition.get(f) == region]
        if members:
            anchor = max(members, key=lambda x: G.degree(x))
            country = G.nodes[anchor].get('country', 'Unknown')
            legend_elements.append(plt.Line2D(
                [0], [0], marker='o', color='w', markersize=10,
                markerfacecolor=region_colors[region],
                label=f"Hub {i + 1}: {size} facilities (anchored in {country})"))

    ax.legend(handles=legend_elements, title="Regional Hubs",
              loc='lower left', fontsize=8)

    plt.savefig(filename, dpi=300, bbox_inches='tight')
    plt.close()
    print(f"Regional hub optimization visualization saved to {filename}")


def _create_world_map_base(figsize=(16, 8), title="Operations Network"):
    """Blank world map to draw points on."""
    fig = plt.figure(figsize=figsize)
    ax = plt.axes(projection=ccrs.PlateCarree())
    ax.set_global()
    ax.add_feature(cfeature.LAND, facecolor='lightgray')
    ax.add_feature(cfeature.OCEAN, facecolor='#d9f2fb')
    ax.add_feature(cfeature.COASTLINE, linewidth=0.5, edgecolor='#888888')
    ax.add_feature(cfeature.BORDERS, linewidth=0.3, linestyle='-', edgecolor='#888888')
    plt.title(title, fontsize=15)
    return fig, ax


def export_facility_metrics(G, throughput_exposure, chokepoint_exposure,
                            influence_exposure, partition, filename=None):
    """Write the per-facility metrics CSV that the dashboards read.

    One row per facility: degree, betweenness, hub label, etc. Column names are
    kept dashboard-friendly (snake_case, no surprises) since Tableau binds to
    them directly.
    """
    filename = filename or _output_path("facility_metrics.csv")
    os.makedirs(os.path.dirname(filename), exist_ok=True)

    in_degree = dict(G.in_degree())
    out_degree = dict(G.out_degree())

    # Total volume = inbound + outbound route weight at the facility. Default
    # weight to 1 in case an edge somehow lacks one.
    def _total_volume(node):
        inbound = sum(d.get('weight', 1) for _, _, d in G.in_edges(node, data=True))
        outbound = sum(d.get('weight', 1) for _, _, d in G.out_edges(node, data=True))
        return inbound + outbound

    rows = []
    for node in G.nodes():
        attrs = G.nodes[node]
        rows.append({
            'facility_iata': node,
            'facility_name': attrs.get('name', 'Unknown'),
            'city': attrs.get('city', 'Unknown'),
            'country': attrs.get('country', 'Unknown'),
            'latitude': attrs.get('latitude'),
            'longitude': attrs.get('longitude'),
            'degree': throughput_exposure.get(node, 0),
            'in_degree': in_degree.get(node, 0),
            'out_degree': out_degree.get(node, 0),
            'betweenness': chokepoint_exposure.get(node, 0.0),
            'influence_score': influence_exposure.get(node, 0.0),
            'total_shipment_volume': _total_volume(node),
            'regional_hub_id': partition.get(node, -1),
        })

    metrics_df = pd.DataFrame(rows)

    # Sort by betweenness so the riskiest chokepoints land at the top, then
    # number them 1..N for an easy "rank" column in the dashboard.
    metrics_df = metrics_df.sort_values('betweenness', ascending=False).reset_index(drop=True)
    metrics_df.insert(0, 'vulnerability_rank', metrics_df.index + 1)

    metrics_df.to_csv(filename, index=False)
    print(f"Tableau-ready facility metrics ({len(metrics_df)} facilities) saved to {filename}")
    return metrics_df


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Loading facility and lane data...")
    facilities_df = load_facilities()
    lanes_df = load_lanes()
    print(f"Loaded {len(facilities_df)} facilities and {len(lanes_df)} lanes")

    print("\nBuilding operations network...")
    G = create_operations_network(facilities_df, lanes_df)
    print(f"Network built with {len(G.nodes())} facilities and {len(G.edges())} lanes")

    print("\nCalculating network statistics...")
    stats = calculate_network_statistics(G)
    print("\nOperations Network Statistics:")
    print(f"Facilities: {stats['num_nodes']}")
    print(f"Lanes: {stats['num_edges']}")
    print(f"Average connectivity: {stats['avg_degree']:.2f}")
    print(f"Network density: {stats['network_density']:.6f}")
    print(f"Average clustering: {stats['avg_clustering']:.4f}")
    print(f"Disconnected clusters: {stats['num_weakly_connected']}")
    print(f"Largest operational cluster: {stats['largest_wcc_size']} "
          f"({stats['largest_wcc_percentage']:.2f}%)")

    plot_throughput_distribution(G)

    print("\nScoring the Network Vulnerability Index...")
    throughput_exp, chokepoint_exp, influence_exp = calculate_network_vulnerability_index(G)

    print("\nTop 10 facilities by chokepoint exposure (Network Vulnerability Index):")
    for i, (facility, value) in enumerate(get_top_facilities(chokepoint_exp, 10), 1):
        name = G.nodes[facility]['name']
        city = G.nodes[facility]['city']
        country = G.nodes[facility]['country']
        print(f"{i}. {facility} ({name}, {city}, {country}): {value:.6f}")

    print("\nRunning Regional Hub Optimization...")
    partition = optimize_regional_hubs(G)
    num_regions = len(set(partition.values()))
    print(f"Network partitioned into {num_regions} regional hubs")

    print("\nRunning disruption resilience stress test...")
    resilience_df = analyze_disruption_resilience(G, top_n=20)
    visualize_disruption_resilience(resilience_df)

    print("\nGenerating regional hub map...")
    visualize_regional_hubs(G, partition)

    print("\nExporting Tableau-ready facility metrics...")
    export_facility_metrics(G, throughput_exp, chokepoint_exp, influence_exp, partition)

    print("\nAll analytics complete. Outputs written to the 'output/' directory.")


if __name__ == "__main__":
    main()
