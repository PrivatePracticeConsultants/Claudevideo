"""Local test double for the two external services the ReferralMap module
talks to: the CMS FOIA download host and the NPPES registry API.

Usage: python3 fake_cms_server.py <port> <site_dir>
  GET /foia/<file>           -> serves static files from <site_dir>
  GET /nppes/?...            -> canned NPPES v2.1 responses (see FIXTURES)
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

SITE = Path(sys.argv[2])

def provider(npi, kind, name_or_names, taxonomies, location_zip, mailing_zip=None,
             primary_desc=None, enumerated='2008-06-15'):
    """taxonomies: list of codes; first one is primary."""
    basic = {'enumeration_date': enumerated}
    if kind == 'NPI-2':
        basic['organization_name'] = name_or_names
    else:
        basic['first_name'], basic['last_name'] = name_or_names
    addresses = [{
        'address_purpose': 'LOCATION', 'city': 'TESTVILLE', 'state': 'MO',
        'postal_code': location_zip,
    }, {
        'address_purpose': 'MAILING', 'city': 'MAILTOWN', 'state': 'MO',
        'postal_code': mailing_zip or location_zip,
    }]
    taxes = []
    for i, code in enumerate(taxonomies):
        taxes.append({'code': code, 'desc': primary_desc or code, 'primary': i == 0})
    return {
        'number': npi, 'enumeration_type': kind, 'basic': basic,
        'addresses': addresses, 'taxonomies': taxes,
    }

# ZIP 99999 fixtures: an org PT clinic, an individual PT, a PT assistant (must
# be excluded), and a provider whose only tie to the ZIP is a MAILING address
# (must be excluded).
IN_ZIP = [
    provider('9000000001', 'NPI-2', 'TEST REHAB CLINIC LLC', ['261QP2000X'], '999991234'),
    provider('9000000002', 'NPI-1', ('PAT', 'THERAPIST'), ['225100000X'], '999990000'),
    provider('9000000003', 'NPI-1', ('ANNE', 'ASSISTANT'), ['225200000X'], '999990000'),
    # Enumerated 2015-11-10 — WITHIN the calendar year but AFTER the 2015 file's
    # ~Sep-1 service cutoff, so it must be flagged "No". (With a naive Dec-31
    # cutoff this would wrongly read "Yes"; guards the data-window-end fix.)
    provider('9000000005', 'NPI-1', ('NEW', 'GRAD'), ['225100000X'], '999990000',
             enumerated='2015-11-10'),
]
MAILING_ONLY = provider('9000000004', 'NPI-2', 'ELSEWHERE PT CENTER', ['261QP2000X'],
                        '111110000', mailing_zip='999990000')
# A malformed short location postal code (these exist in the live registry):
# must not crash discovery. Reachable only via a prefix search like 999*.
SHORT_POSTAL = provider('9000000006', 'NPI-1', ('SHORT', 'ZIPCODE'), ['225100000X'], '9999')

# ZIP 88888: a paging fixture — 201 individual PTs so the client must fetch a
# full 200-row page and then a second page.
PAGED = [provider(f'86{i:08d}', 'NPI-1', ('PT', f'PAGE{i}'), ['225100000X'], '888880000')
         for i in range(201)]

# Source providers looked up by number= during enrichment.
SOURCES = {
    '8000000001': provider('8000000001', 'NPI-1', ('DAVID', 'DOCTOR'), ['207Q00000X'],
                           '999990000', primary_desc='Family Medicine'),
    '8000000002': provider('8000000002', 'NPI-1', ('OLIVIA', 'ORTHO'), ['207X00000X'],
                           '999990000', primary_desc='Orthopaedic Surgery'),
    # 8000000003 deliberately absent -> "(NPI deactivated or not found)"
}

# ZIP 77777: individual therapists for the PracticeGroups tests. Two belong to a
# named multi-member group, one is solo (its group has a blank name).
PG_THERAPISTS = [
    provider('7011111111', 'NPI-1', ('JOSE', 'MEMBERONE'), ['225100000X'], '777770000'),
    provider('7022222222', 'NPI-1', ('PAT', 'MEMBERTWO'), ['225100000X'], '777770000'),
    provider('7044444444', 'NPI-1', ('SAM', 'SOLO'), ['225100000X'], '777770000'),
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path.startswith('/foia/'):
            f = SITE / url.path[len('/foia/'):]
            if f.is_file():
                data = f.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.end_headers()
            return
        if url.path.rstrip('/').endswith('/nppes'):
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if 'number' in q:
                hit = SOURCES.get(q['number'])
                for p in IN_ZIP + [MAILING_ONLY, SHORT_POSTAL] + PAGED + PG_THERAPISTS:
                    if p['number'] == q['number']:
                        hit = p
                results = [hit] if hit else []
                self._json({'result_count': len(results), 'results': results})
                return
            # Name search (used by Find-RmPractice): organization_name for
            # orgs, last_name for individuals; optional state filter. Mirrors
            # NPPES contains/wildcard behavior loosely.
            if 'organization_name' in q or 'last_name' in q:
                term = (q.get('organization_name') or q.get('last_name', '')).lower().rstrip('*')
                state = q.get('state', '')
                want_org = 'organization_name' in q
                results = []
                for p in IN_ZIP + [MAILING_ONLY, SHORT_POSTAL] + PG_THERAPISTS:
                    b = p['basic']
                    name = b.get('organization_name') if want_org else b.get('last_name')
                    if not name or term not in name.lower():
                        continue
                    loc_states = [a['state'] for a in p['addresses']
                                  if a['address_purpose'] == 'LOCATION']
                    if state and state not in loc_states:
                        continue
                    results.append(p)
                self._json({'result_count': len(results), 'results': results})
                return
            # ZIP matching mirrors NPPES: the fixture's ZIP must START WITH the
            # query prefix (query '999*' matches fixtures in 99999).
            prefix = q.get('postal_code', '').rstrip('*')
            term = q.get('taxonomy_description', '')
            skip = int(q.get('skip', '0'))
            results = []
            if term == 'Physical Therapy' and '99999'.startswith(prefix[:5]) and skip == 0:
                results = IN_ZIP + [MAILING_ONLY, SHORT_POSTAL]
            elif term == 'Physical Therapist' and '88888'.startswith(prefix[:5]):
                results = PAGED[skip:skip + 200]
            elif term == 'Physical Therapist' and '77777'.startswith(prefix[:5]) and skip == 0:
                results = PG_THERAPISTS
            self._json({'result_count': len(results), 'results': results})
            return
        self.send_response(404)
        self.end_headers()


if __name__ == '__main__':
    HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
