#!/usr/bin/env python3
"""Reproducible, labeled API evaluation of synthetic invoices.

Uses Python stdlib only. This script makes REAL authenticated uploads and may
incur Gemini API charges. It never prints or saves credentials.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib import error, request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'evaluation' / 'fixtures'
LABELS = ROOT / 'evaluation' / 'labels.json'
RESULTS = ROOT / 'evaluation' / 'results'
FIELDS = ('invoice_number','invoice_date','supplier_name','customer_name','currency','subtotal','tax','total')
ITEM_FIELDS = ('description','quantity','unit_price','line_total')
MONEY_FIELDS = {'subtotal','tax','total','quantity','unit_price','line_total'}
TERMINAL = {'completed', 'failed'}


def normalize(value, field):
    if value is None:
        return None
    if field in MONEY_FIELDS:
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return ('invalid', str(value))
    return ' '.join(str(value).split()).casefold()


def percent(n, denom):
    return round(100 * n / denom, 2) if denom else None


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    import math
    return round(ordered[max(0, math.ceil(p * len(ordered)) - 1)], 4)


def call_json(url, token, *, payload=None, content_type=None, timeout=100):
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    method = 'GET'
    if payload is not None:
        method = 'POST'
        headers['Content-Type'] = content_type or 'application/json'
    req = request.Request(url, data=payload, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            result = resp.read(4 * 1024 * 1024)
            return json.loads(result)
    except error.HTTPError as exc:
        body = exc.read(1024).decode('utf-8', 'replace')
        # A returned server message might include content echoed from inputs;
        # never log the supplied token.
        body = body.replace(token, '[REDACTED]')
        raise RuntimeError(f'HTTP {exc.code}: {body[:400]}') from None
    except error.URLError as exc:
        raise RuntimeError(f'Network connection error: {getattr(exc, "reason", "unavailable")}') from None


def upload(url, token, file_path, timeout):
    boundary = 'cg-' + uuid4().hex
    # Files are deliberately tiny synthetic fixtures; bounded in-memory upload.
    body = (
        (f'--{boundary}\r\n'
         f'Content-Disposition: form-data; name="files"; filename="{file_path.name}"\r\n'
         f'Content-Type: application/octet-stream\r\n\r\n').encode()
        + file_path.read_bytes()
        + f'\r\n--{boundary}--\r\n'.encode()
    )
    return call_json(url + '/api/documents/upload', token, payload=body,
                     content_type=f'multipart/form-data; boundary={boundary}', timeout=timeout)


def field_matches(expected, actual):
    details = {}
    total = 0
    matched = 0
    for key in FIELDS:
        if expected.get(key) is None:
            continue
        total += 1
        ok = normalize(expected[key],key) == normalize(actual.get(key),key)
        matched += int(ok)
        details[key] = {'match': ok, 'expected': expected[key], 'actual': actual.get(key)}
    expected_items=expected.get('line_items',[])
    actual_items=actual.get('line_items',[])
    for index, item in enumerate(expected_items):
        got = actual_items[index] if index < len(actual_items) else {}
        for key in ITEM_FIELDS:
            if item.get(key) is None:
                continue
            total += 1
            ok = normalize(item[key],key) == normalize(got.get(key),key)
            matched += int(ok)
            details[f'line_items.{index}.{key}'] = {'match':ok, 'expected':item[key], 'actual':got.get(key)}
    return matched, total, details


def evaluate_case(base_url, token, filename, label, request_timeout, deadline_seconds, interval):
    p=FIXTURES/filename
    record={'fixture': filename, 'format': label['format'], 'state':'not_started',
            'accepted': False, 'duration_seconds':None,'matches':0,'scored_fields':0}
    try:
        # Do not include Render wake-up and initial health in per-document latency.
        response=upload(base_url,token,p,request_timeout)
        candidates=response.get('documents',[])
        if len(candidates)!=1 or not candidates[0].get('id'):
            raise RuntimeError('Upload response lacks exactly one document id')
        doc_id=candidates[0]['id']
        record['document_id']=doc_id
        record['accepted']=True
        start=time.monotonic()  # time since HTTP 202 acknowledgement
        status=None
        while time.monotonic()-start < deadline_seconds:
            meta=call_json(base_url+f'/api/documents/{doc_id}',token,timeout=request_timeout)
            status=meta.get('status','unknown')
            if status in TERMINAL:
                break
            time.sleep(interval)
        record['duration_seconds']=round(time.monotonic()-start,4)
        record['state']=status if status in TERMINAL else 'timeout'
        if record['state']=='failed':
            record['error_code']=meta.get('error_code')
            record['error_message']=meta.get('error_message')
        if record['state']=='completed':
            invoice=call_json(base_url+f'/api/documents/{doc_id}/invoice',token,timeout=request_timeout)
            result=invoice.get('result',{})
            match,total,details=field_matches(label['expected'], result.get('invoice',{}))
            record.update(matches=match,scored_fields=total,field_results=details,
                          provider=result.get('provider'),model=result.get('model'),
                          review_required=bool(result.get('review_required')),
                          issue_count=len(result.get('issues',[])),
                          errors=sum(1 for x in result.get('issues',[]) if x.get('severity')=='error'),
                          warnings=sum(1 for x in result.get('issues',[]) if x.get('severity')=='warning'))
    except Exception as exc:
        # The evaluator must distinguish client/network errors from terminal backend failures.
        record['client_error']=str(exc).replace(token,'[REDACTED]')[:500]
        if record['state'] not in TERMINAL:
            record['state']='client_error'
    return record


def summarize(cases, started_at, base):
    accepted=[x for x in cases if x['accepted']]
    complete=[x for x in accepted if x['state']=='completed']
    failed=[x for x in accepted if x['state']=='failed']
    timed_out=[x for x in accepted if x['state']=='timeout']
    client_errors=[x for x in cases if x['state']=='client_error']
    durations=[x['duration_seconds'] for x in complete if x['duration_seconds'] is not None]
    matches=sum(x['matches'] for x in complete)
    scored=sum(x['scored_fields'] for x in complete)
    # Field accuracy denominator includes every labeled field in an accepted document,
    # including failed/timeouts; report completed-only accuracy separately.
    total_expected=sum(sum(1 for f in FIELDS if x.get('expected',{}).get(f) is not None) +
                       sum(sum(1 for key in ITEM_FIELDS if item.get(key) is not None)
                           for item in x.get('expected',{}).get('line_items',[]))
                       for x in (LABEL_DOCS[f['fixture']] for f in accepted))
    return {
        'evaluated_at_utc':started_at,
        'base_url':base,
        'dataset':'Care Guide synthetic invoices v1',
        'requested_cases':len(cases), 'accepted_count':len(accepted),
        'completed_count':len(complete),'terminal_failed_count':len(failed),
        'timeout_count':len(timed_out),'client_error_count':len(client_errors),
        'completion_rate_pct':percent(len(complete),len(accepted)),
        'terminal_failure_rate_pct':percent(len(failed),len(accepted)),
        'timeout_rate_pct':percent(len(timed_out),len(accepted)),
        'completed_only_field_accuracy_pct':percent(matches,scored),
        'all_accepted_field_accuracy_pct':percent(matches,total_expected),
        'correct_fields':matches,'scored_fields_completed':scored,
        'known_fields_accepted':total_expected,
        'mean_completion_seconds':round(statistics.mean(durations),4) if durations else None,
        'median_completion_seconds':round(statistics.median(durations),4) if durations else None,
        'p95_completion_seconds':percentile(durations,0.95),
        'review_required_completed_count':sum(bool(x.get('review_required')) for x in complete),
        'total_issues_completed':sum(x.get('issue_count',0) for x in complete),
        'cases':cases,
    }


def render_markdown(summary):
    def show(val, suffix=''):
        return 'Not measured' if val is None else str(val)+suffix
    lines=[
        '# Measured synthetic-document evaluation',
        '', '> Auto-generated from **actual authenticated API responses**. This is a small, synthetic set, not a production accuracy claim.',
        '',f'- Evaluated at (UTC): `{summary["evaluated_at_utc"]}`',
        f'- Backend: `{summary["base_url"]}`',
        f'- Dataset: `{summary["dataset"]}`',
        '', '## Metrics', '',
        '| Metric | Measured value |','|---|---:|',
        f'| Accepted documents | {summary["accepted_count"]}/{summary["requested_cases"]} |',
        f'| Completed | {summary["completed_count"]} |',
        f'| Terminal failed | {summary["terminal_failed_count"]} |',
        f'| Timed out | {summary["timeout_count"]} |',
        f'| Client errors | {summary["client_error_count"]} |',
        f'| Completion rate (accepted) | {show(summary["completion_rate_pct"],"%") } |',
        f'| Terminal failure rate (accepted) | {show(summary["terminal_failure_rate_pct"],"%") } |',
        f'| Completed-only field accuracy | {show(summary["completed_only_field_accuracy_pct"],"%") } ({summary["correct_fields"]}/{summary["scored_fields_completed"]}) |',
        f'| All-accepted field accuracy | {show(summary["all_accepted_field_accuracy_pct"],"%") } ({summary["correct_fields"]}/{summary["known_fields_accepted"]}) |',
        f'| Mean end-to-end completion time | {show(summary["mean_completion_seconds"]," s") } |',
        f'| Median end-to-end completion time | {show(summary["median_completion_seconds"]," s") } |',
        f'| p95 completion time | {show(summary["p95_completion_seconds"]," s") } |',
        f'| Documents requiring review | {summary["review_required_completed_count"]} |',
        f'| Total validation issues | {summary["total_issues_completed"]} |',
        '', '## By-file outcomes','',
        '| Synthetic file | Format | Status | Field matches | Processing seconds | Warnings |',
        '|---|---|---|---:|---:|---:|',
    ]
    for c in summary['cases']:
        lines.append(f'| `{c["fixture"]}` | {c["format"]} | {c["state"]} | '
                     f'{c["matches"]}/{c["scored_fields"]} | {show(c.get("duration_seconds"))} | {c.get("warnings","—")} |')
    lines += ['', '## Per-field discrepancies','']
    any_diff=False
    for case in summary['cases']:
        for field, detail in case.get('field_results',{}).items():
            if not detail['match']:
                any_diff=True
                lines.append(f'- `{case["fixture"]}` / `{field}`: expected `{detail["expected"]}`, returned `{detail["actual"]}`.')
        if case.get('client_error'):
            any_diff=True
            lines.append(f'- `{case["fixture"]}`: {case["client_error"]}')
    if not any_diff:
        lines.append('No field discrepancies observed among the completed samples. This does not imply zero generalization error.')
    lines += ['', '## Method and limitations','',
              'Each case was uploaded with an authorized HTTP POST, then status-polled until `completed`, `failed` or timeout. Duration starts **after upload acknowledgement**, includes queue/network/poll overhead and is not pure Gemini latency. A missing expected field counts as an incorrect extraction. Numeric comparisons use `Decimal`; text is casefolded and whitespace-normalized. Each example contains one line item; item-order differences can lower this simple metric.',
              '', 'The dataset is only five handcrafted **synthetic** invoices. It is not representative of real-world customers or 1,000-page documents. Cross-file reconciliation, semantic search, role-based permissions and durable Render Free storage are outside this evaluation. For fuller validity, add independent natural layouts, actual reference annotations, larger samples, and multi-page stress measurements.',
              '', 'Source: `evaluation/fixtures/`, `evaluation/labels.json`, `tools/evaluate_live.py`, and same-run `evaluation/results/live-evaluation.json`.']
    return '\n'.join(lines)+'\n'


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default=os.getenv('DOCUMENT_BASE_URL','https://ai-document-intelligence-api-cjh6.onrender.com'))
    parser.add_argument('--limit',type=int,default=5,help='Number of files to upload (1..5)')
    parser.add_argument('--request-timeout',type=int,default=100,help='HTTP request timeout seconds')
    parser.add_argument('--job-timeout',type=int,default=360,help='Per-document wait limit seconds')
    parser.add_argument('--poll-interval',type=float,default=4.0)
    parser.add_argument('--validate-only',action='store_true',help='Check fixture/label manifest without network access')
    args=parser.parse_args(argv)
    global LABEL_DOCS
    payload=json.loads(LABELS.read_text(encoding='utf-8'))
    LABEL_DOCS=payload['cases']
    assert 1<=args.limit<=len(LABEL_DOCS), 'limit outside available labeled fixture count'
    for name,data in LABEL_DOCS.items():
        assert (FIXTURES/name).is_file(), f'Missing fixture: {name}'
        assert (FIXTURES/name).stat().st_size>0, f'Empty fixture: {name}'
        assert all(field in data['expected'] for field in FIELDS), f'Incomplete labels: {name}'
    if args.validate_only:
        print('VALID: %d labeled synthetic fixture files.' % len(LABEL_DOCS))
        return 0
    token=os.getenv('DOCUMENT_API_TOKEN','') or getpass.getpass('Demo API token (not echoed): ')
    if not token:
        print('ERROR: missing API token',file=sys.stderr)
        return 2
    base=args.base_url.rstrip('/')
    if not base.startswith('https://') and not (base.startswith('http://127.0.0.1:') or base.startswith('http://localhost:')):
        print('ERROR: HTTPS is required outside localhost',file=sys.stderr)
        return 2
    print('Checking authorized API access (credentials hidden)...')
    check=call_json(base+'/api/auth/check',token,timeout=args.request_timeout)
    if check.get('authenticated') is not True:
        print('ERROR: authentication check failed',file=sys.stderr)
        return 2
    started_at=datetime.now(timezone.utc).isoformat()
    samples=[]
    for name,label in list(LABEL_DOCS.items())[:args.limit]:
        print(f'Uploading labeled synthetic fixture: {name}', flush=True)
        case=evaluate_case(base,token,name,label,args.request_timeout,args.job_timeout,args.poll_interval)
        samples.append(case)
        print(f'  {case["state"]} | {case["matches"]}/{case["scored_fields"]} matched | {case.get("duration_seconds")} sec',flush=True)
    report=summarize(samples,started_at,base)
    RESULTS.mkdir(parents=True,exist_ok=True)
    (RESULTS/'live-evaluation.json').write_text(json.dumps(report,indent=2,default=str)+'\n',encoding='utf-8')
    (RESULTS/'live-evaluation.md').write_text(render_markdown(report),encoding='utf-8')
    print(f'Report saved: {RESULTS / "live-evaluation.md"}')
    print('Field accuracy among completed:',report['completed_only_field_accuracy_pct'])
    print('Completion rate:', report['completion_rate_pct'])
    return 0

if __name__=='__main__':
    sys.exit(main())
