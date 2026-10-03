"""Read-only MCP server over the wildlife sighting log (tags/wildlife.db).

Tools list sightings by date / species / station / time of day, summarise
them, and pull the captures (images, video clips) behind a sighting. Nothing
here writes: the DB is opened ``mode=ro`` and the only files created are
regenerable preview JPEGs under derived/.
"""
