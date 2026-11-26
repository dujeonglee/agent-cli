"""Recovery primitive toolbox for the robust-harness design.

See ``docs/robust-harness/DESIGN.md`` for the conceptual model. Primitives
are pure, harness-level functions: they take normalized inputs (strings,
dicts, registries) and return Intervention text fragments. They never
reference provider names, model names, or channel names — that
abstraction lives in the Provider Layer.
"""
