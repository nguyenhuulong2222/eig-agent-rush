# Fictional Kanaloa County Elections site (static). Results are served from /srv/site/data,
# which compose mounts read-only from the county_data volume.
FROM python:3.12-slim AS build
WORKDIR /src
COPY public/ public/
COPY results/ results/
COPY scripts/ scripts/
RUN python scripts/build_site.py --root .

FROM python:3.12-slim
COPY --from=build /src/public/ /srv/site/
# The data dir seeds the county_data volume. Owned by the orchestrator's uid (10001) so only the
# orchestrator can restore results; county-site (uid 10002) mounts the volume read-only anyway.
RUN chown -R 10001:10001 /srv/site/data
RUN useradd --system --uid 10002 --no-create-home county
USER county
EXPOSE 8080
CMD ["python", "-m", "http.server", "8080", "--directory", "/srv/site"]
