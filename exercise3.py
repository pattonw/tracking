# ---
# jupyter:
#   jupytext:
#     formats: py:percent,ipynb
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.14.1
#   kernelspec:
#     display_name: Python 3 (ipykernel)
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Exercise 3/3: Tracking with an integer linear program (ILP)
#
# You could also run this notebook on your laptop, a GPU is not needed :).
#
# Here we will introduce a modern formulation of tracking-by-detection.
#     
# You will learn
# - how linking with global context can be modeled as a **network flow** using `networkx` and solved efficiently as an **integer linear program (ILP)** with `cvxpy` for small-scale problems (Exercise 3.1).
# - to adapt the formulation from Exercise 3.1 to allow for **arbitrary track starting and ending points** (Exercise 3.2).
# - to extend the ILP to properly model **cell divisions** (Exercise 3.3).
# - to tune the **hyperparameters** of the ILP (Exercise 3.4).
#
# Places where you are expected to write code are marked with ```YOUR CODE HERE```.

# %% [markdown] tags=[] jp-MarkdownHeadingCollapsed=true jp-MarkdownHeadingCollapsed=true tags=[]
# ## Import packages

# %%
# Force keras to run on CPU
import os
os.environ["CUDA_VISIBLE_DEVICES"] = ""

# Notebook at full width in the browser
from IPython.display import display, HTML
display(HTML("<style>.container { width:100% !important; }</style>"))

import sys
from urllib.request import urlretrieve
from pathlib import Path
from collections import defaultdict
from abc import ABC, abstractmethod

import matplotlib
import matplotlib.pyplot as plt
# %matplotlib inline
matplotlib.rcParams["image.interpolation"] = "none"
matplotlib.rcParams['figure.figsize'] = (12, 6)
from tifffile import imread, imwrite
from tqdm.auto import tqdm
import skimage
import pandas as pd
import scipy

from stardist import fill_label_holes, random_label_cmap
from stardist.plot import render_label
from stardist.models import StarDist2D
from stardist import _draw_polygons
from csbdeep.utils import normalize
import numpy as np
import networkx as nx
import cvxpy as cp

import napari
import networkx as nx

lbl_cmap = random_label_cmap()
# Pretty tqdm progress bars 
# ! jupyter nbextension enable --py widgetsnbextension

# %% [markdown]
# Some utility functions

# %%
def plot_img_label(img, lbl, img_title="image", lbl_title="label", **kwargs):
    fig, (ai,al) = plt.subplots(1,2, gridspec_kw=dict(width_ratios=(1,1)))
    im = ai.imshow(img, cmap='gray', clim=(0,1))
    ai.set_title(img_title)
    ai.axis("off")
    al.imshow(render_label(lbl, img=.3*img, normalize_img=False, cmap=lbl_cmap))
    al.set_title(lbl_title)
    al.axis("off")
    plt.tight_layout()
    
def preprocess(X, Y, axis_norm=(0,1)):
    # normalize channels independently
    X = np.stack([normalize(x, 1, 99.8, axis=axis_norm) for x in tqdm(X, leave=True, desc="Normalize images")])
    # fill holes in labels
    Y = np.stack([fill_label_holes(y) for y in tqdm(Y, leave=True, desc="Fill holes in labels")])
    return X, Y


# %% [markdown] tags=[] jp-MarkdownHeadingCollapsed=true
# ## Inspect the dataset

# %% [markdown]
# For this exercise we will be working with an even smaller crop of the dataset we have already used in the tracking exercises 1 and 2.
#
# It is a fluorenscence microscopy time-lapse of breast cancer cells with stained nuclei (SiR-DNA), similar to the dataset at  https://zenodo.org/record/4034976#.YwZRCJPP1qt.

# %%
base_path = Path("data/exercise3")

# %% [markdown]
# Load the dataset (images and tracking annotations) from disk into this notebook.

# %%
x = np.stack([imread(str(p)) for p in sorted((base_path/ "images").glob("*.tif"))])
y = np.stack([imread(str(p)) for p in sorted((base_path/ "gt_tracking").glob("*.tif"))])
assert len(x) == len(x)
print(f"Number of images: {len(x)}")
print(f"Image shape: {x[0].shape}")
links = pd.read_csv(base_path / "gt_tracking" / "man_track.txt", names=["track_id", "from", "to", "parent_id"], sep=" ")
print("Links")
links[:10]

# %%
x, y = preprocess(x, y)

# %% [markdown]
# Visualize some images (by changing `idx`).

# %%
idx = 0
plot_img_label(x[idx], y[idx])

# %%
viewer = napari.Viewer()
viewer.add_image(x, name="image");


# %% [markdown] tags=[]
# <div class="alert alert-block alert-danger"><h3>Napari in a jupyter notebook:</h3>
#     
# - To have napari working in a jupyter notebook, you need to use up-to-date versions of napari, pyqt and pyqt5, as is the case in the conda environments provided together with this exercise.
# - When you are coding and debugging, close the napari viewer with `viewer.close()` to avoid problems with the two event loops of napari and jupyter.
# - **If a cell is not executed (empty square brackets on the left of a cell) despite you running it, running it a second time right after will usually work.**
# </div>

# %% [markdown]
# Let's add the ground truth annotations. Now we can easily explore how the cells move over time.
#
# If you zoom in, you will note that the dense annotations are not perfect segmentations, but rather circles placed roughly in the center of each nucleus.

# %%
def visualize_tracks(viewer, y, links=None, name=""):
    """Utility function to visualize segmentation and tracks"""
    max_label = max(links.max(), y.max()) if links is not None else y.max()
    colorperm = np.random.default_rng(42).permutation(np.arange(1, max_label + 2))
    tracks = []
    for t, frame in enumerate(y):
        centers = skimage.measure.regionprops(frame)
        for c in centers:
            tracks.append([colorperm[c.label], t, int(c.centroid[0]), int(c.centroid[1])])
    tracks = np.array(tracks)
    tracks = tracks[tracks[:, 0].argsort()]
    
    graph = {}
    if links is not None:
        divisions = links[links[:,3] != 0]
        for d in divisions:
            if colorperm[d[0]] not in tracks[:, 0] or colorperm[d[3]] not in tracks[:, 0]:
                continue
            graph[colorperm[d[0]]] = [colorperm[d[3]]]

    viewer.add_labels(y, name=f"{name}_detections")
    viewer.layers[f"{name}_detections"].contour = 3
    viewer.add_tracks(tracks, name=f"{name}_tracks", graph=graph)
    return tracks


# %%
visualize_tracks(viewer, y, links.to_numpy(), "ground_truth");

# %% [markdown] tags=[] jp-MarkdownHeadingCollapsed=true
# ## Object detection using a pre-trained neural network

# %% [markdown] tags=[]
# Load a pretrained stardist model, detect nuclei in one image and visualize them.

# %%
idx = 0
model = StarDist2D(None, name="stardist_breast_cancer", basedir="models")
(detections, details), (prob, _) = model.predict_instances(x[idx], scale=(1, 1), nms_thresh=0.3, prob_thresh=0.3, return_predict=True)
plot_img_label(x[idx], detections, lbl_title="detections")

# %% [markdown]
# Here we visualize in detail the polygons and probabbility maps we have detected with StarDist.
# <!-- Notice that each object comes with a center point, which we can use to comnms_thresh=ise eprob_thresh= distances between objects. -->

# %%
coord, points, polygon_prob = details['coord'], details['points'], details['prob']
plt.figure()
plt.subplot(121)
plt.title("Predicted Polygons")
_draw_polygons(coord, points, polygon_prob, show_dist=True)
plt.imshow(x[idx], cmap='gray'); plt.axis('off')

plt.subplot(122)
plt.title("Object center probability")
plt.imshow(prob, cmap='magma'); plt.axis('off')
plt.tight_layout()
plt.show() 

# %% [markdown]
# Detect centers and segment nuclei in all images of the time lapse.

# %%
prob_thres = 0.3
nms_thres = 0.6
scale = (1.0, 1.0)
pred = [model.predict_instances(xi, show_tile_progress=False, scale=scale, nms_thresh=nms_thres, prob_thresh=prob_thres, return_predict=True)
              for xi in tqdm(x)]
detections = np.array([xi[0][0] for xi in pred])
centers = [xi[0][1]["points"] for xi in pred]
center_probs = [xi[0][1]["prob"] for xi in pred]
prob_maps = np.stack([xi[1][0] for xi in pred])

# %% [markdown]
# Visualize the dense detections. Note that they are still not linked and therefore randomly colored.

# %%
viewer = napari.viewer.current_viewer()
if viewer:
    viewer.close()
viewer = napari.Viewer()
viewer.add_image(x)
visualize_tracks(viewer, y, links.to_numpy(), "ground_truth");
viewer.add_labels(detections, name=f"detections_scale_{scale}_nmsthres_{nms_thres}");
# viewer.add_image(prob_maps, colormap="magma", scale=(2,2), opacity=0.2);
viewer.grid.enabled = True


# %% [markdown] jp-MarkdownHeadingCollapsed=true tags=[]
# ## Build a candidate graph from the detections

# %% [markdown]
#  $G = (V,E)$ 

# %%
def build_graph(detections, max_distance, detection_probs=None, drift=(0,0)):
    """Builds a networkx graph from dense detection arrays.
    
    Edge weights are drift-corrected euclidian distances of detection centroids.
        
    Args:
        detections: list of dense 2D arrays.
        max_distance (int): Only connect vertices whose distance is less than max_distance.
        detection_probs: list of arrays, corresponding to ordered ids in detections.
        drift: Constant drift correction for each pair of frames.
        
    Returns:
        A networkx directed graph,
        A list of lookup tables that maps detections in each frame to global node ids.
    """
    G = nx.DiGraph()
    n_v = 0
    
    luts = []
    draw_positions = {}
    
    for t, d in enumerate(detections):
        frame = skimage.segmentation.relabel_sequential(d)[0]
        regions = skimage.measure.regionprops(frame)
        lut = {}
        for i, r in enumerate(regions):
            draw_pos = np.array([t, d.shape[0] - r.centroid[0]])
            weight = detection_probs[t][i] if detection_probs else 1
            G.add_node(n_v, time=t, detection_id=r.label, weight=weight, draw_position=draw_pos)
            draw_positions[n_v] = draw_pos
            lut[r.label] = n_v
            n_v += 1
        luts.append(lut)

    n_e = 0
    for t, (d0, d1) in enumerate(zip(detections, detections[1:])):
        f0 = skimage.segmentation.relabel_sequential(d0)[0]
        r0 = skimage.measure.regionprops(f0)
        c0 = [np.array(r.centroid) for r in r0]

        f1 = skimage.segmentation.relabel_sequential(d1)[0]
        r1 = skimage.measure.regionprops(f1)
        c1 = [np.array(r.centroid) for r in r1]

        for _r0, _c0 in zip(r0, c0):
            for _r1, _c1 in zip(r1, c1):
                dist = np.linalg.norm(_c0 - _c1)
                if dist < max_distance:
                    G.add_edge(
                        luts[t][_r0.label],
                        luts[t+1][_r1.label],
                        # normalized euclidian distance
                        weight = np.linalg.norm(_c0 + np.array(drift) - _c1) / max_distance,
                        edge_id = n_e,
                    )
                    n_e += 1
    
    return G, luts


# %%
def build_graph_from_tracks(detections, links=None):
    """
    
    Args:
        detections: list of dense 2D arrays.
        links: (n, 4)-array with columns track_id, from, to, parent_id.
        
    Returns:
        A networkx directed graph,
        A list of lookup tables that maps detections in each frame to global node ids.
    """
    G = nx.DiGraph()
    n_v = 0
    
    luts = []
    draw_positions = {}
    
    for t, d in enumerate(detections):
        frame = d
        regions = skimage.measure.regionprops(frame)
        lut = {}
        for r in regions:
            draw_pos = np.array([t, d.shape[0] - r.centroid[0]])
            G.add_node(n_v, time=t, detection_id=r.label, weight=1, draw_position=draw_pos)
            draw_positions[n_v] = draw_pos
            lut[r.label] = n_v
            n_v += 1
        luts.append(lut)
        
    n_e = 0
    for t, (d0, d1) in enumerate(zip(detections, detections[1:])):
        f0 = d0
        r0 = skimage.measure.regionprops(f0)
        c0 = [np.array(r.centroid) for r in r0]

        f1 = d1
        r1 = skimage.measure.regionprops(f1)
        c1 = [np.array(r.centroid) for r in r1]

        for _r0, _c0 in zip(r0, c0):
            for _r1, _c1 in zip(r1, c1):
                if _r0.label == _r1.label:
                    G.add_edge(
                        luts[t][_r0.label],
                        luts[t+1][_r1.label],
                        # normalized euclidian distance
                        weight = np.linalg.norm(_c0 - _c1),
                        edge_id = n_e,
                    )
                    n_e += 1
    
    if links is not None:
        divisions = links[links[:,3] != 0]
        for d in divisions:
            if d[1] > 0 and d[1] < detections.shape[0]:
                try:
                    G.add_edge(luts[d[1] - 1][d[3]], luts[d[1]][d[0]])
                except KeyError:
                    pass
    
    return G, luts


# %%
def draw_graph(g, title=None, ax=None, height=None):
    """
    Args:
        g: networkx Graph.
        title: plot title.
    
    """
    pos = {i: g.nodes[i]["draw_position"] for i in g.nodes}
    if ax is None:
        _, ax = plt.subplots()
    ax.set_title(title)
    nx.draw(g, pos=pos, with_labels=True, ax=ax)

    ax.set_axis_on()
    ax.tick_params(left=True, bottom=True, labelleft=True, labelbottom=True)
    if height:
        ax.set_ylim(0, height)
    
    ax.set_xlabel("time")
    ax.set_ylabel("y (spatial)");


# %% [markdown]
# We compare the candidate graph to the ground truth graph. Note that the vertical position of each node corresponds to the vertical position of the detection in the image (in napari).
#
# The candidate graph has both more edges and more nodes than the ground truth graph. Now we need an algorithm to prune the candidate graph.

# %%
gt_graph, gt_luts = build_graph_from_tracks(y, links.to_numpy())
candidate_graph, candidate_luts = build_graph(detections, max_distance=50, detection_probs=center_probs, drift=(-6 , 0))

# %%
fig, (ax0, ax1) = plt.subplots(1,2, figsize=(24, 12))
draw_graph(gt_graph, "Ground truth graph", ax=ax0, height=detections[0].shape[0])
draw_graph(candidate_graph, "Candidate graph", ax=ax1, height=detections[0].shape[0])


# %% [markdown] jp-MarkdownHeadingCollapsed=true tags=[]
# ## Network flow

# %% [markdown]
# As hinted earlier, our goal is to prune the candidate graph. More formally we want to find a graph $\tilde{G}=(\tilde{V}, \tilde{E})$ whose vertices $\tilde{V}$ are a subset of the candidate graph vertices $V$ and whose edges $\tilde{E}$ are a subset of the candidate graph edges $E$.
#
# The first algorithm we will use to do this is a [network flow](https://en.wikipedia.org/wiki/Network_flow_problem). It adds a source and a target vertex to the graph and tries to find as many disjunct paths from source to target as possible. All other vertices and edges are discarded. This specific algorithm is called maximum flow.
#
#
# Finding a good subgraph $\tilde{G}=(\tilde{V}, \tilde{E})$ can be formulated as an [integer linear program (ILP)](https://en.wikipedia.org/wiki/Integer_programming) (also, refer to the tracking lecture slides), where we assign a binary variable $x$ and a cost $c$ to each vertex and edge in $G$, and then computing $min_c c^Tx$. A set of linear constraints ensure that the solution will be a feasible graph. For example, if an edge is part of $\tilde{G}$, both its incident nodes have to be part of $\tilde{G}$ as well.
#
# Here we express the network flow as an ILP using `cvxpy`. We have already written two of the three constraints for you.

# %% [markdown] tags=[] jp-MarkdownHeadingCollapsed=true
# ## Exercise 3.1
# <div class="alert alert-block alert-info"><h3>Exercise 3.1: Write the flow constraint of the network flow</h3>
# In words, this means that if a node in $\tilde{G}$ has an incoming edge, it must also have an outgoing edge. Don't forget the edges to the source and target nodes. The flow constraint does not apply to the source and target nodes.
# </div>

# %%
def graph2ilp_flow(graph, hyperparams):
    """"""
    
    # Extend the graph with a single appear and death node
    graph_flow = nx.DiGraph()
    graph_flow.add_node("appear", weight=0)
    graph_flow.add_node("death", weight=0)
    
    for n, time in graph.nodes(data="time"):
        if time == 0:
            # Connect all nodes in initial frame to appear node
            graph_flow.add_node(n, weight=0)
            graph_flow.add_edge("appear", n, weight=0)
        elif time == len(detections) - 1:
            # Connect all nodes in last frame to death node
            graph_flow.add_node(n, weight=0)
            graph_flow.add_edge(n, "death", weight=0)
        
    edge_to_idx = {edge: i for i, edge in enumerate(graph.edges)}
    edge_to_idx_flow = {edge: i for i, edge in enumerate(graph_flow.edges)}

    E = graph.number_of_edges()
    V = graph.number_of_nodes()
    E_flow = graph_flow.number_of_edges()
    x = cp.Variable(E + V + E_flow, boolean=True)
    
    c_e = hyperparams["edge_factor"] * np.array([graph.get_edge_data(*e)["weight"] for e in graph.edges])
    c_v = hyperparams["node_offset"] + hyperparams["node_factor"] * np.array([v for k, v in graph.nodes(data="weight")])
    c_e_flow = hyperparams["edge_factor"] * np.array([graph_flow.get_edge_data(*e)["weight"] for e in graph_flow.edges])  # weight set to 0 above
    
    c = np.concatenate([c_e, c_v, c_e_flow])
    
    # constraint matrices: {E or V} x (E + V + E_flow)
    # columns: c_e, c_v, c_e_flow
    
    # Consistency constraint edges
    A0 = np.zeros((E, E + V + E_flow))
    A0[:E, :E] = 2 * np.eye(E)
    for edge in graph.edges:
        edge_id = edge_to_idx[edge]
        A0[edge_id, E + edge[0]] = -1
        A0[edge_id, E + edge[1]] = -1
    
    # Consistency constraint nodes
    A1 = np.zeros((V, E + V + E_flow))
    A1[:V, E:E+V] = 2 * np.eye(V)
    for node in graph.nodes:
        for edge in graph.in_edges(node):
            edge_id = edge_to_idx[edge]
            A1[node, edge_id] = -1
        # Edge from appear to node    
        if node in graph_flow.nodes:
            for edge in graph_flow.in_edges(node):
                edge_id = edge_to_idx_flow[edge]
                A1[node, E + V + edge_id] = -1
         
        for edge in graph.out_edges(node):
            edge_id = edge_to_idx[edge]
            A1[node, edge_id] = -1
        # Edge from node to death
        if node in graph_flow.nodes:
            for edge in graph_flow.out_edges(node):
                edge_id = edge_to_idx_flow[edge]
                A1[node, E+V+edge_id] = -1
            
    # Flow constraint
    A2 = np.zeros((V, E + V + E_flow))
    
    ### YOUR CODE HERE ###
    
    constraints = [
        A0 @ x <= 0, 
        A1 @ x == 0,
        A2 @ x == 0,
    ]
    
    objective = cp.Minimize( c.T @ x)

    return cp.Problem(objective, constraints)


# %%
ilp_flow = graph2ilp_flow(candidate_graph, hyperparams={"node_factor": -1, "edge_factor": 1})

# %%
ilp_flow.solve()
E = candidate_graph.number_of_edges()
V = candidate_graph.number_of_nodes()
print("ILP Status: ", ilp_flow.status)
print("The optimal value is", ilp_flow.value)
print("x_e")
print(ilp_flow.variables()[0].value[:E])
print("x_v")
print(ilp_flow.variables()[0].value[E:E+V])
print("x_e_flow")
print(ilp_flow.variables()[0].value[E+V:])


# %%
def solution2graph(solution, base_graph):
    
    solution_var = solution.variables()[0].value
    
    new_graph = nx.DiGraph()
    
    # Build nodes
    x_v = solution_var[E:E+V]
    picked_nodes = (x_v > 1e-6).nonzero()[0]  # small epsilon for solution variables
    for node in picked_nodes:
        node_features = base_graph.nodes[node]
        new_graph.add_node(node, **node_features)
    
    # Build edges
    original_edges = list(base_graph.edges)
    x_e = solution_var[:E]
    picked_edges = (x_e > 1e-6).nonzero()[0]
    for edge in picked_edges:
        new_graph.add_edge(*original_edges[edge])
    return new_graph


# %%
solved_graph_flow = solution2graph(ilp_flow, candidate_graph)

# %%
fig, (ax0, ax1, ax2) = plt.subplots(1,3, figsize=(32, 12))
draw_graph(gt_graph, "Ground truth graph", ax=ax0, height=detections[0].shape[0])
draw_graph(candidate_graph, "Candidate graph", ax=ax1, height=detections[0].shape[0])
draw_graph(solved_graph_flow, f"Network flow (no divisions) - cost: {ilp_flow.value:.3f}", ax=ax2, height=detections[0].shape[0])


# %% [markdown]
# ### Recolor detections in napari according to solution

# %%
def recolor_detections(detections, graph, node_luts):
    """
    Args:
        detections: list of dense 2d integer arrays.
        graph: networkx graph.
        node_luts: List of lookup tables from detections in each frame to global node ids.
    
    Returns
    
        array with consistently index detections according to the input graph.
    """
    assert len(detections) == len(node_luts)
    
    out = []
    n_tracks = 1
    color_lookup_tables = []
    
    for t in tqdm(range(0, len(detections)), desc="Recoloring detections"):
        new_frame = np.zeros_like(detections[t])
        color_lut = {}
        for det_id, node_id in node_luts[t].items():
            if node_id not in graph.nodes:
                continue
            edges = graph.in_edges(node_id)
            if not edges:
                new_frame[detections[t] == graph.nodes[node_id]["detection_id"]] = n_tracks
                color_lut[graph.nodes[node_id]["detection_id"]] = n_tracks
                n_tracks += 1
            else:
                for v_tm1, u_t0 in edges:
                    new_frame[detections[t] == graph.nodes[u_t0]["detection_id"]] = color_lookup_tables[t-1][graph.nodes[v_tm1]["detection_id"]]
                    color_lut[graph.nodes[u_t0]["detection_id"]] = color_lookup_tables[t-1][graph.nodes[v_tm1]["detection_id"]]
                
        color_lookup_tables.append(color_lut)
        out.append(new_frame)
        

    return np.stack(out)

# %%
recolored_gt = recolor_detections(y, gt_graph, gt_luts)
detections_ilp_flow = recolor_detections(detections=detections, graph=solved_graph_flow, node_luts=candidate_luts)

viewer = napari.viewer.current_viewer()
if viewer:
    viewer.close()
viewer = napari.Viewer()
viewer.add_image(x)
# visualize_tracks(viewer, y)
viewer.add_labels(recolored_gt)
viewer.add_labels(detections)
viewer.add_labels(detections_ilp_flow)
viewer.grid.enabled = True


# %% [markdown]
# ## Checkpoint 1
# <div class="alert alert-block alert-success"><h3>Checkpoint 1: We have familiarized ourselves with the formulation of an ILP for linking and and have a feasible solution to a network flow.</h3></div>

# %% [markdown] tags=[] jp-MarkdownHeadingCollapsed=true
# ## Exercise 3.2
# <div class="alert alert-block alert-info"><h3>Exercise 3.2: Extend the network flow from Exercise 3.1 such that tracks can start and end at arbitrary time points.</h3>
# The code is very similar to exercise 3.1. This time all constraints are already given.  
# </div>

# %%
def graph2ilp_nodiv(graph, hyperparams):
    """"""
    
    # Extend the graph with a single appear and death node
    graph_flow = nx.DiGraph()
    graph_flow.add_node("appear", weight=0)
    graph_flow.add_node("death", weight=0)
    
    
    ### YOUR CODE HERE ###
        
        
    edge_to_idx = {edge: i for i, edge in enumerate(graph.edges)}
    edge_to_idx_flow = {edge: i for i, edge in enumerate(graph_flow.edges)}

    E = graph.number_of_edges()
    V = graph.number_of_nodes()
    E_flow = graph_flow.number_of_edges()
    x = cp.Variable(E + V + E_flow, boolean=True)
    
    c_e = hyperparams["edge_factor"] * np.array([graph.get_edge_data(*e)["weight"] for e in graph.edges])
    c_v = hyperparams["node_factor"] * np.array([v for k, v in graph.nodes(data="weight")])
    c_e_flow = np.array([graph_flow.get_edge_data(*e)["weight"] for e in graph_flow.edges])

    c = np.concatenate([c_e, c_v, c_e_flow])
    
    # constraint matrices: {E or V} x (E + V + E_flow)
    # columns: c_e, c_v, c_e_flow
    
    # Edge consistency constraint
    A0 = np.zeros((E, E + V + E_flow))
    A0[:E, :E] = 2 * np.eye(E)
    for edge in graph.edges:
        edge_id = edge_to_idx[edge]
        A0[edge_id, E + edge[0]] = -1
        A0[edge_id, E + edge[1]] = -1
    
    # Node consistency constraint
    A1 = np.zeros((V, E + V + E_flow))
    A1[:V, E:E+V] = 2 * np.eye(V)
    for node in graph.nodes:
        for edge in graph.in_edges(node):
            edge_id = edge_to_idx[edge]
            A1[node, edge_id] = -1
        if node in graph_flow.nodes:
            for edge in graph_flow.in_edges(node):
                edge_id = edge_to_idx_flow[edge]
                A1[node, E + V + edge_id] = -1
         
        for edge in graph.out_edges(node):
            edge_id = edge_to_idx[edge]
            A1[node, edge_id] = -1
        if node in graph_flow.nodes:
            for edge in graph_flow.out_edges(node):
                edge_id = edge_to_idx_flow[edge]
                A1[node, E+V+edge_id] = -1
            
    # Network flow constraint
    A2 = np.zeros((V, E + V + E_flow))
    for node in graph.nodes:
        for edge in graph.in_edges(node):
            edge_id = edge_to_idx[edge]
            A2[node, edge_id] = -1
        if node in graph_flow.nodes:
            for edge in graph_flow.in_edges(node):
                edge_id = edge_to_idx_flow[edge]
                A2[node, E+V+edge_id] = -1
            
            
        for edge in graph.out_edges(node):
            edge_id = edge_to_idx[edge]
            A2[node, edge_id] = 1   
        if node in graph_flow.nodes:
            for edge in graph_flow.out_edges(node):
                edge_id = edge_to_idx_flow[edge]
                A2[node, E+V+edge_id] = 1
    
    constraints = [
        A0 @ x <= 0, 
        A1 @ x == 0,
        A2 @ x == 0,
    ]
    
    objective = cp.Minimize( c.T @ x)

    return cp.Problem(objective, constraints)

# %%
ilp_nodiv = graph2ilp_nodiv(candidate_graph, hyperparams={"cost_appear": 0.5, "cost_disappear": 0.5, "node_factor": -1, "edge_factor": 1})

# %%
ilp_nodiv.solve()
print("ILP Status: ", ilp_nodiv.status)
print("The optimal value is", ilp_nodiv.value)
print("x_e")
E = candidate_graph.number_of_edges()
V = candidate_graph.number_of_nodes()
print(ilp_nodiv.variables()[0].value[:E])
print("x_v")
print(ilp_nodiv.variables()[0].value[E:E+V])
print("x_e_flow")
print(ilp_nodiv.variables()[0].value[E+V:])

# %%
solved_graph_nodiv = solution2graph(ilp_nodiv, candidate_graph)

# %%
fig, (ax0, ax1, ax2) = plt.subplots(1,3, figsize=(32, 12))
draw_graph(gt_graph, "Ground truth graph", ax=ax0, height=detections[0].shape[0])
draw_graph(candidate_graph, "Candidate graph", ax=ax1, height=detections[0].shape[0])
draw_graph(solved_graph_nodiv, f"ILP solution (no divisions) - cost: {ilp_nodiv.value:.3f}", ax=ax2, height=detections[0].shape[0])

# %%
recolored_gt = recolor_detections(y, gt_graph, gt_luts)
detections_ilp_nodiv = recolor_detections(detections=detections, graph=solved_graph_nodiv, node_luts=candidate_luts)

viewer = napari.viewer.current_viewer()
if viewer:
    viewer.close()
viewer = napari.Viewer()
viewer.add_image(x)
# visualize_tracks(viewer, y)
viewer.add_labels(recolored_gt)
viewer.add_labels(detections)
viewer.add_labels(detections_ilp_nodiv)
viewer.grid.enabled = True


# %% [markdown]
# ## ILP model including divisions

# %% [markdown] jp-MarkdownHeadingCollapsed=true tags=[]
# ## Exercise 3.3
# <div class="alert alert-block alert-info"><h3>Exercise 3.3: Complete yet another extension of the ILP such that it allows for cell divisions.</h3>
# The constraint matrices A0, A1 and A2 are identical to Exercise 3.2, with the important difference that A1 and A2 are changed to inequality constraints here.
# This model needs one more constraint to lead to feasible solutions, we call it the *split constraint*.
# Please write the final constraint and think about why it is needed.
# </div>
#
# If you're unsure, you can refer to Malin-Mayor et al. (2021) for inspiration. But be aware that their whole formulation of the ILP is slightly different.
#
# [Malin-Mayor, Caroline, et al. "Automated reconstruction of whole-embryo cell lineages by learning from sparse annotations." bioRxiv (2021).](https://www.biorxiv.org/content/10.1101/2021.07.28.454016v1.abstract)

# %%
def graph2ilp_div(graph, hyperparams):
    """"""
    
    # Extend the graph with a single appear and death node
    graph_flow = nx.DiGraph()
    graph_flow.add_node("appear", weight=0)
    graph_flow.add_node("death", weight=0)
    
    for n, time in graph.nodes(data="time"):
        graph_flow.add_node(n, weight=0)
        graph_flow.add_edge("appear", n, weight=hyperparams["cost_appear"])
        graph_flow.add_node(n, weight=0)
        graph_flow.add_edge(n, "death", weight=hyperparams["cost_disappear"])
        
        
    edge_to_idx = {edge: i for i, edge in enumerate(graph.edges)}
    edge_to_idx_flow = {edge: i for i, edge in enumerate(graph_flow.edges)}

    E = graph.number_of_edges()
    V = graph.number_of_nodes()
    E_flow = graph_flow.number_of_edges()
    x = cp.Variable(E + V + E_flow, boolean=True)
    
    c_e = hyperparams["edge_factor"] * np.array([graph.get_edge_data(*e)["weight"] for e in graph.edges])
    c_v = hyperparams["node_factor"] * np.array([v for k, v in graph.nodes(data="weight")])
    c_e_flow = np.array([graph_flow.get_edge_data(*e)["weight"] for e in graph_flow.edges])

    c = np.concatenate([c_e, c_v, c_e_flow])
    
    # constraint matrices: {E or V} x (E + V + E_flow)
    # columns: c_e, c_v, c_e_flow
    
    # Edge consistency constraint
    A0 = np.zeros((E, E + V + E_flow))
    A0[:E, :E] = 2 * np.eye(E)
    for edge in graph.edges:
        edge_id = edge_to_idx[edge]
        A0[edge_id, E + edge[0]] = -1
        A0[edge_id, E + edge[1]] = -1
    
    # Node consistency constraint
    A1 = np.zeros((V, E + V + E_flow))
    A1[:V, E:E+V] = 2 * np.eye(V)
    for node in graph.nodes:
        for edge in graph.in_edges(node):
            edge_id = edge_to_idx[edge]
            A1[node, edge_id] = -1
        if node in graph_flow.nodes:
            for edge in graph_flow.in_edges(node):
                edge_id = edge_to_idx_flow[edge]
                A1[node, E + V + edge_id] = -1
         
        for edge in graph.out_edges(node):
            edge_id = edge_to_idx[edge]
            A1[node, edge_id] = -1
        if node in graph_flow.nodes:
            for edge in graph_flow.out_edges(node):
                edge_id = edge_to_idx_flow[edge]
                A1[node, E+V+edge_id] = -1
            
    # Network flow constraint
    A2 = np.zeros((V, E + V + E_flow))
    for node in graph.nodes:
        for edge in graph.in_edges(node):
            edge_id = edge_to_idx[edge]
            A2[node, edge_id] = 1   
        if node in graph_flow.nodes:
            for edge in graph_flow.in_edges(node):
                edge_id = edge_to_idx_flow[edge]
                A2[node, E + V+ edge_id] = 1
            
            
        for edge in graph.out_edges(node):
            edge_id = edge_to_idx[edge]
            A2[node, edge_id] = -1      
        if node in graph_flow.nodes:
            for edge in graph_flow.out_edges(node):
                edge_id = edge_to_idx_flow[edge]
                A2[node, E + V + edge_id] = -1
    
    # split constraint
    A3 = np.zeros((V, E + V + E_flow))
    
    ### YOUR CODE HERE ###
    
    
    constraints = [
        A0 @ x <= 0, 
        A1 @ x <= 0,
        A2 @ x <= 0,
        A3 @ x <= 0,
    ]
    
    objective = cp.Minimize( c.T @ x)
    
    return cp.Problem(objective, constraints)

# %%
ilp_div = graph2ilp_div(candidate_graph, hyperparams={"cost_appear": 0.15, "cost_disappear": 0.5, "node_offset": 0, "node_factor": -1, "edge_factor": 0.4})

# %%
ilp_div.solve()
print("ILP Status: ", ilp_div.status)
print("The optimal value is", ilp_div.value)
print("x_e")
E = candidate_graph.number_of_edges()
V = candidate_graph.number_of_nodes()
print(ilp_div.variables()[0].value[:E])
print("x_v")
print(ilp_div.variables()[0].value[E:E+V])
print("x_e_flow")
print(ilp_div.variables()[0].value[E+V:])

# %%
solved_graph_div = solution2graph(ilp_div, candidate_graph)

# %%
det_solved_div = recolor_detections(detections=detections, graph=solved_graph_div, node_luts=candidate_luts)

# %%
viewer = napari.viewer.current_viewer()
if viewer:
    viewer.close()
viewer = napari.Viewer()
viewer.add_image(x)
viewer.add_labels(recolored_gt)
viewer.add_labels(detections)
viewer.add_labels(det_solved_div)
viewer.grid.enabled = True

# %%
fig, ((ax0, ax1), (ax2, ax3)) = plt.subplots(2,2, figsize=(24, 16))
draw_graph(candidate_graph, "Candidate graph", ax=ax0)
draw_graph(solved_graph_div, f"ILP solution (with divisions) - cost: {ilp_div.value:.3f}", ax=ax1)
draw_graph(gt_graph, "Ground truth graph", ax=ax2)
draw_graph(solved_graph_nodiv, f"ILP solution (no divisions) - cost: {ilp_nodiv.value:.3f}", ax=ax3)


# %% [markdown] jp-MarkdownHeadingCollapsed=true tags=[]
# ## Exercise 3.4
# <div class="alert alert-block alert-info"><h3>Exercise 3.4: Try to improve the ILP-based tracking from exercise 3.3</h3>
#
# For example
# - Tune the hyperparameters.
# - Better edge features than drift-corrected euclidian distance.
# - Tune the detection algorithm to avoid false negatives.
#     
# </div>

# %%
