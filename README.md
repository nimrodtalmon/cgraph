# Contribution Graph

Science as a graph of contributions instead of a pile of papers.

- **Contributions** are nodes (problem, assumption, model, theorem, algorithm, dataset, experiment, claim) and arcs (one node builds on another, e.g. an experiment *validates* a model).
- **Curators** accept contributions. Accepting an arc also accepts its two ends; un-accepting a node also removes its accepted arcs.
- **Front** of a curator: accepted nodes that no accepted arc points to. Exportable as JSON.
- **Samples**: random graphs of their own (`?g=s-…`), separate from the main graph.

Live: https://talmonlab.bgu.ac.il/cgraph/

## Run

    python server.py 4801        # Python 3.8+, standard library only; data in $DATA (default ./data)

Deployed on talmonlab (see `server/apps.json` in nimrodtalmon/talmonlab); a push here is live within about 5 minutes.
Vendored: cytoscape 3.30.2, dagre 0.8.5, cytoscape-dagre 2.5.0 (MIT; licenses in `vendor/`).
