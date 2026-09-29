"""Community / unified-inbox engine (Work 09).

Lane B owns ``sync.py`` (COMMUNITY_SYNC durable ingestion); Lane C adds the
sibling classification/drafting/action modules alongside it. Keep this
package init minimal — job-handler registration lives in the submodule and
requires that submodule to be imported at startup.
"""
