"""Nova's semantic layer, which is Cube.

Each source's skill holds a Cube data model (`skills/<slug>/model/<slug>.yml`) and an access
manifest (`model/access.json`) that Cube's `queryRewrite` enforces. The model is drafted by the
profiler and checked here before Cube ever loads it; the manifest is only ever written by code.
"""
