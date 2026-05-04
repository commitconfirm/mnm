"""CSV and JSON export endpoints for filtered plugin querysets (E6).

Each list view's filter bar surfaces "Export CSV" and "Export
JSON" buttons that hit these endpoints with the same query
params the list view consumed. The endpoint runs the same
filterset over the model's queryset and streams results without
pagination.

Hard cap: ``EXPORT_LIMIT`` (50,000 rows) per export. Beyond
that, the operator gets a 413 response with a "refine your
filter" message. v1.0 does not paginate exports.

Streaming via :class:`StreamingHttpResponse` to keep memory
bounded — :meth:`QuerySet.iterator` yields one row at a time,
the writer flushes to the response without buffering the entire
result in memory.
"""

from __future__ import annotations

import csv
import json
from typing import Iterable

from django.http import (
    HttpResponseNotFound,
    JsonResponse,
    StreamingHttpResponse,
)

from mnm_plugin import filters, models
from mnm_plugin.api import serializers


EXPORT_LIMIT = 50_000


# Model + filterset + serializer per URL-path slug. Keys match
# the list-view URL slugs in ``urls.py``.
_EXPORTABLE = {
    "endpoints": (
        models.Endpoint,
        filters.EndpointFilterSet,
        serializers.EndpointSerializer,
    ),
    "arp-entries": (
        models.ArpEntry,
        filters.ArpEntryFilterSet,
        serializers.ArpEntrySerializer,
    ),
    "mac-entries": (
        models.MacEntry,
        filters.MacEntryFilterSet,
        serializers.MacEntrySerializer,
    ),
    "lldp-neighbors": (
        models.LldpNeighbor,
        filters.LldpNeighborFilterSet,
        serializers.LldpNeighborSerializer,
    ),
    "routes": (
        models.Route,
        filters.RouteFilterSet,
        serializers.RouteSerializer,
    ),
    "bgp-neighbors": (
        models.BgpNeighbor,
        filters.BgpNeighborFilterSet,
        serializers.BgpNeighborSerializer,
    ),
    "fingerprints": (
        models.Fingerprint,
        filters.FingerprintFilterSet,
        serializers.FingerprintSerializer,
    ),
}


class _Echo:
    """File-like stand-in for ``csv.writer``'s destination.

    csv.writer expects a writable; its ``write()`` returns
    whatever we pass — which lets ``StreamingHttpResponse`` stream
    lines without intermediate buffering.
    """

    def write(self, value):
        return value


def _filtered_queryset(request, model_key: str):
    """Run the filterset and enforce the 50k cap.

    Returns ``(queryset, error_response)`` — ``error_response`` is
    ``None`` on success; else a 4xx ``HttpResponse`` for the
    caller to return verbatim.
    """
    spec = _EXPORTABLE.get(model_key)
    if not spec:
        return None, HttpResponseNotFound(f"Unknown model: {model_key}")
    model_class, filterset_class, _ = spec
    base_qs = model_class.objects.all()
    filterset = filterset_class(request.GET, queryset=base_qs)
    queryset = filterset.qs
    count = queryset.count()
    if count > EXPORT_LIMIT:
        return None, JsonResponse(
            {
                "error": (
                    f"Too many rows ({count:,}); refine your filter "
                    f"({EXPORT_LIMIT:,} max)."
                ),
            },
            status=413,
        )
    return queryset, None


def _serialized_rows(queryset, serializer_class, request) -> Iterable[dict]:
    context = {"request": request}
    for instance in queryset.iterator():
        yield serializer_class(instance, context=context).data


def _csv_value(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return json.dumps(v, default=str)
    return str(v)


def export_csv(request, model_key: str):
    """Stream a CSV of the filtered queryset."""
    queryset, error = _filtered_queryset(request, model_key)
    if error is not None:
        return error
    _, _, serializer_class = _EXPORTABLE[model_key]

    rows = _serialized_rows(queryset, serializer_class, request)
    # Peek at the first row to derive column headers. If the
    # queryset is empty, fall back to concrete model fields so the
    # CSV still has a header line.
    try:
        first_row = next(iter(rows))
        columns = list(first_row.keys())
    except StopIteration:
        first_row = None
        columns = [f.name for f in queryset.model._meta.concrete_fields]

    pseudo_buffer = _Echo()
    writer = csv.writer(pseudo_buffer)

    def _generate():
        yield writer.writerow(columns)
        if first_row is not None:
            yield writer.writerow(
                [_csv_value(first_row.get(c)) for c in columns],
            )
        for row in rows:
            yield writer.writerow(
                [_csv_value(row.get(c)) for c in columns],
            )

    response = StreamingHttpResponse(
        _generate(), content_type="text/csv",
    )
    filename = f"mnm_{model_key.replace('-', '_')}.csv"
    response["Content-Disposition"] = (
        f'attachment; filename="{filename}"'
    )
    return response


def export_json(request, model_key: str):
    """Stream a JSON list of the filtered queryset."""
    queryset, error = _filtered_queryset(request, model_key)
    if error is not None:
        return error
    _, _, serializer_class = _EXPORTABLE[model_key]

    def _generate():
        yield "["
        first = True
        for row in _serialized_rows(queryset, serializer_class, request):
            if not first:
                yield ","
            else:
                first = False
            yield json.dumps(row, default=str)
        yield "]"

    response = StreamingHttpResponse(
        _generate(), content_type="application/json",
    )
    filename = f"mnm_{model_key.replace('-', '_')}.json"
    response["Content-Disposition"] = (
        f'attachment; filename="{filename}"'
    )
    return response
