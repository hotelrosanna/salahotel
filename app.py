from flask import Flask, request, send_file, render_template_string, jsonify
import pdfplumber
import re
import io
import os
import traceback
from datetime import datetime
from collections import defaultdict

from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_CENTER, TA_LEFT

app = Flask(__name__)

# ─────────────────────────────────────────────
# PARSING — Lista Pasti Giornalieri
# ─────────────────────────────────────────────

def parse_lista_pasti(pdf_bytes):
    """
    Returns (rows_list, pdf_date).
    rows_list is an ordered list of dicts — multiple rows per camera are supported.
    Notes from Arrivi x Sala appear inline in the lista pasti as short keywords
    (e.g. 'blu', 'BLU', 'cane') right before the 5 numbers.
    Notes that span multiple lines appear AFTER the camera row they belong to.
    """
    rows = []
    pdf_date = None

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full_text = ""
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                full_text += t + "\n"

    date_match = re.search(r'Data dal (\d{2}/\d{2}/\d{4})', full_text)
    if date_match:
        pdf_date = date_match.group(1)

    lines = [l.strip() for l in full_text.split('\n')]

    skip_prefixes = ('Data dal', 'Tipo', 'Individuale', 'Totale', 'Lista pasti',
                     'LORENZO', 'Informazioni', '1/', '2/')

    cam_start   = re.compile(r'^(\d{3}(?:\s+\d+)?|park\s*\d*)-(.+)', re.IGNORECASE)
    nums_suffix = re.compile(r'(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$')

    def is_skip(line):
        return not line or any(line.startswith(p) for p in skip_prefixes)

    i = 0
    while i < len(lines):
        line = lines[i]
        if is_skip(line):
            i += 1
            continue

        m_start = cam_start.match(line)
        if not m_start:
            i += 1
            continue

        cam_raw = m_start.group(1).strip()
        rest    = m_start.group(2).strip()

        # Normalise camera: keep only digits for standard rooms, skip 'park' entries
        if re.match(r'^\d{3}$', cam_raw):
            cam = cam_raw
        else:
            # park or non-standard — skip
            i += 1
            continue

        # Find the 5 numbers — they may be on this line or the next
        m_nums = nums_suffix.search(rest)
        if m_nums:
            name_and_note = rest[:m_nums.start()].strip()
            i += 1
        else:
            # Look ahead for numbers line
            j = i + 1
            while j < len(lines) and is_skip(lines[j]):
                j += 1
            if j < len(lines):
                next_line = lines[j]
                m_nums = nums_suffix.search(next_line)
                if m_nums:
                    extra = next_line[:m_nums.start()].strip()
                    # extra might be a word that's part of the name (e.g. "Wieslaw")
                    # only treat it as name continuation if it's a single word with no spaces
                    if extra and not re.match(r'^[A-Za-zÀ-ÿ\-]+$', extra):
                        name_and_note = rest  # don't append ambiguous text
                    else:
                        name_and_note = (rest + ' ' + extra).strip() if extra else rest
                    i = j + 1
                else:
                    i += 1
                    continue
            else:
                i += 1
                continue

        # Extract inline note keywords from the end of name_and_note
        # These are short words/phrases that are NOT part of the guest name
        # Note keywords — ordered so "N keyword" variants come before bare keywords
        note_inline_pattern = re.compile(
            r'\s+(intollerante\s+\S+'
            r'|\d+\s+cani|\d+\s+celiac[oi]|\d+\s+allergi[a-z]*'
            r'|cani|cane|celiac[oi]|allergi[a-z]*|vegano?|vegetariano?'
            r'|disabile[^0-9]*|[Gg]luten[^0-9]*|senza\s+lattosio[^0-9]*|Colazione[^0-9]*'
            r'|[Bb][Ll][Uu])\s*$',
            re.IGNORECASE
        )
        note_inline = ''
        m_note = note_inline_pattern.search(name_and_note)
        if m_note:
            note_inline = name_and_note[m_note.start():].strip()
            name_and_note = name_and_note[:m_note.start()].strip()

        # Collect note lines that follow this camera entry
        note_lines = [note_inline] if note_inline else []
        while i < len(lines):
            peek = lines[i]
            if is_skip(peek):
                i += 1
                continue
            if cam_start.match(peek):
                break
            if nums_suffix.search(peek):
                i += 1
                continue
            # Single word/name fragment (letters, commas, dots) = name continuation, not a note
            if re.match(r'^[A-Za-zÀ-ÿ,.\'\-]+$', peek):
                i += 1
                continue
            note_lines.append(peek)
            i += 1

        note_text = ' '.join(note_lines).strip()

        rows.append({
            'cam':            cam,
            'camera_ref':     f"{cam}-{name_and_note}",
            'note_soggiorno': note_text,
            'arrivi_pasti':   m_nums.group(1),
            'casa':           m_nums.group(2),
            'partenze_pasti': m_nums.group(3),
            'colaz':          m_nums.group(4),
            'cena':           m_nums.group(5),
        })

    # Post-process: merge notes between consecutive rows of the same camera
    for idx in range(len(rows) - 1):
        if rows[idx]['cam'] == rows[idx+1]['cam']:
            combined = ' '.join(filter(None, [rows[idx]['note_soggiorno'],
                                               rows[idx+1]['note_soggiorno']])).strip()
            rows[idx]['note_soggiorno']   = ''
            rows[idx+1]['note_soggiorno'] = combined

    return rows, pdf_date


# ─────────────────────────────────────────────
# PARSING — Arrivi x Sala
# ─────────────────────────────────────────────

def parse_arrivi_sala(pdf_bytes):
    """
    Returns dict: cam -> list of row dicts.
    Handles:
    - cambio camera (↳ symbol): new-camera row kept, old-camera row dropped
    - same prenotazione on multiple cameras (group booking): ALL kept
    - same camera with different prenotazioni (check-out + check-in): ALL kept
    """
    def parse_date(s):
        try:
            return datetime.strptime((s or '').strip().lstrip('\u21b3').strip(), '%d/%m/%Y')
        except Exception:
            return datetime.min

    all_rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if not (row and len(row) == 13):
                        continue
                    if not (row[0] and str(row[0]).startswith('BN') and row[1]):
                        continue
                    cam  = str(row[1]).strip()
                    pren = str(row[0]).strip()
                    # Skip non-standard camera entries (e.g. "park 1")
                    if not re.match(r'^\d{3}$', cam):
                        continue
                    arrivo_raw   = str(row[4] or '')
                    is_cambio    = arrivo_raw.startswith('\u21b3')
                    arrivo_clean = arrivo_raw.lstrip('\u21b3').strip()

                    all_rows.append({
                        'cam':     cam,
                        'cam_ref': str(row[2] or '').lstrip('\u2605').strip(),
                        'n_pren':  pren,
                        'n':       str(row[3] or ''),
                        'arrivo':  arrivo_clean,
                        'partenza':str(row[5] or ''),
                        'ad':      str(row[6] or ''),
                        'b':       str(row[7] or ''),
                        'b_0_1':   str(row[8] or ''),
                        'b_2_4':   str(row[9] or ''),
                        'b_5_7':   str(row[10] or ''),
                        'b_8_11':  str(row[11] or ''),
                        'tariffa': str(row[12] or ''),
                        'cambio':  is_cambio,
                    })

    # Step 1: for cambio rows, copy ad/b from the corresponding old-camera row
    cambio_prens = {r['n_pren'] for r in all_rows if r['cambio']}
    for r in all_rows:
        if r['cambio']:
            for other in all_rows:
                if other['n_pren'] == r['n_pren'] and not other['cambio'] and other['cam'] != r['cam']:
                    r['ad']     = other['ad']
                    r['b']      = other['b']
                    r['b_0_1']  = other['b_0_1']
                    r['b_2_4']  = other['b_2_4']
                    r['b_5_7']  = other['b_5_7']
                    r['b_8_11'] = other['b_8_11']
                    break

    # Step 2: identify old-camera rows of a cambio booking
    # OLD-camera row = same pren as a cambio row, DIFFERENT camera, NOT itself a cambio
    cambio_new_cams = {r['cam']: r['n_pren'] for r in all_rows if r['cambio']}
    def is_old_cam_row(r):
        if r['cambio']:
            return False
        if r['n_pren'] not in cambio_prens:
            return False
        # It's old only if there's a cambio row for this pren on a DIFFERENT camera
        for other in all_rows:
            if other['cambio'] and other['n_pren'] == r['n_pren'] and other['cam'] != r['cam']:
                return True
        return False

    filtered = [r for r in all_rows if not is_old_cam_row(r)]

    # Step 3: group by camera — keep ALL rows (different prens = different guests)
    by_cam = defaultdict(list)
    for r in filtered:
        by_cam[r['cam']].append(r)

    return dict(by_cam)


# ─────────────────────────────────────────────
# MERGE
# ─────────────────────────────────────────────

def _name_similarity(name_a, name_b):
    """True if two name strings share at least one significant word."""
    if not name_a or not name_b:
        return False
    clean = lambda s: s.strip().lstrip('\u2605').strip().lower()
    words_a = {w for w in clean(name_a).split() if len(w) > 2}
    words_b = {w for w in clean(name_b).split() if len(w) > 2}
    return bool(words_a & words_b)


def merge_data(pasti_rows, arrivi):
    """
    Match each Lista Pasti row to the best Arrivi x Sala row by name similarity.
    Falls back to latest-departure row when no name match found.
    """
    def parse_date(s):
        try:
            return datetime.strptime((s or '').strip(), '%d/%m/%Y')
        except Exception:
            return datetime.min

    merged = []
    for p in sorted(pasti_rows,
                    key=lambda x: (int(x['cam']) if x['cam'].isdigit() else 9999,
                                   pasti_rows.index(x))):
        cam      = p['cam']
        cam_rows = arrivi.get(cam, [])

        a = {}
        if cam_rows:
            pasti_name = p['camera_ref'].split('-', 1)[1] if '-' in p['camera_ref'] else ''
            matched = next((r for r in cam_rows
                            if _name_similarity(pasti_name, r.get('cam_ref', ''))), None)
            if matched:
                a = matched
            else:
                # Fallback: row with latest departure
                a = max(cam_rows, key=lambda r: parse_date(r.get('partenza', '')))

        merged.append({
            'camera_ref':     p['camera_ref'],
            'note_soggiorno': p['note_soggiorno'],
            'arrivi':         p['arrivi_pasti'],
            'casa':           p['casa'],
            'partenze':       p['partenze_pasti'],
            'colaz':          p['colaz'],
            'cena':           p['cena'],
            'n':              a.get('n', ''),
            'arrivo':         a.get('arrivo', ''),
            'partenza':       a.get('partenza', ''),
            'ad':             a.get('ad', ''),
            'b':              a.get('b', ''),
            'b_0_1':          a.get('b_0_1', ''),
            'b_2_4':          a.get('b_2_4', ''),
            'b_5_7':          a.get('b_5_7', ''),
            'b_8_11':         a.get('b_8_11', ''),
        })
    return merged


# ─────────────────────────────────────────────
# PDF GENERATION
# ─────────────────────────────────────────────

def generate_pdf(merged, data_str, table_groups=None):
    buf = io.BytesIO()
    PAGE = landscape(A4)
    W, H = PAGE
    MARGIN = 10 * mm

    doc = SimpleDocTemplate(buf, pagesize=PAGE,
                            leftMargin=MARGIN, rightMargin=MARGIN,
                            topMargin=12*mm, bottomMargin=10*mm)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('Title', parent=styles['Normal'],
                                 fontSize=13, fontName='Helvetica-Bold',
                                 textColor=colors.HexColor('#1a2e4a'), spaceAfter=2)
    sub_style = ParagraphStyle('Sub', parent=styles['Normal'],
                               fontSize=8, fontName='Helvetica',
                               textColor=colors.HexColor('#555555'), spaceAfter=4)
    cell_style = ParagraphStyle('Cell', parent=styles['Normal'],
                                fontSize=7, fontName='Helvetica', leading=9, wordWrap='LTR')
    cell_bold  = ParagraphStyle('CellBold', parent=cell_style, fontName='Helvetica-Bold')
    cell_note  = ParagraphStyle('CellNote', parent=cell_style, fontSize=6.5,
                                textColor=colors.HexColor('#b03030'),
                                fontName='Helvetica-Oblique')

    col_widths = [55*mm, 38*mm, 11*mm, 11*mm, 11*mm, 11*mm, 11*mm,
                  8*mm, 20*mm, 20*mm, 10*mm, 8*mm, 8*mm, 8*mm, 8*mm, 8*mm]

    def h(txt, bold=False):
        return Paragraph(str(txt) if txt else '', cell_bold if bold else cell_style)

    def num(v):
        if v in ('', None):
            return h('-')
        try:
            return h(v) if int(v) > 0 else h('-')
        except:
            return h(v)

    HDR_BG  = colors.HexColor('#1a2e4a')
    HDR_TXT = colors.white

    def hdr(txt):
        return Paragraph(f'<b>{txt}</b>',
                         ParagraphStyle('hdr', parent=cell_style,
                                        fontName='Helvetica-Bold',
                                        textColor=HDR_TXT, fontSize=7,
                                        alignment=TA_CENTER))

    header = [h('Camera e Nominativo', bold=True),
              h('Note soggiorno', bold=True),
              hdr('Arr.'), hdr('Casa'), hdr('Part.'), hdr('Col.'), hdr('Cena'),
              hdr('N'), hdr('Arrivo'), hdr('Partenza'),
              hdr('Ad.'), hdr('B.'), hdr('0-1'), hdr('2-4'), hdr('5-7'), hdr('8-11')]

    ARRIVE_COLOR = colors.HexColor('#e8f5e9')
    DEPART_COLOR = colors.HexColor('#fff3e0')
    WHITE        = colors.white

    GROUP_COLORS = [
        colors.HexColor('#1565c0'), colors.HexColor('#b71c1c'),
        colors.HexColor('#4a148c'), colors.HexColor('#e65100'),
        colors.HexColor('#006064'), colors.HexColor('#33691e'),
        colors.HexColor('#880e4f'), colors.HexColor('#4e342e'),
    ]
    cam_group_color = {}
    if table_groups:
        for g_idx, group in enumerate(table_groups):
            col = GROUP_COLORS[g_idx % len(GROUP_COLORS)]
            for cam in group:
                cam_group_color[cam.strip().zfill(3)] = col

    rows_data  = [header]
    row_colors = []

    for i, r in enumerate(merged):
        is_arrival   = r['arrivi'] not in ('', '0')
        is_departure = r['partenze'] not in ('', '0')
        is_only_dep  = is_departure and not is_arrival and r['casa'] in ('', '0')

        bg = ARRIVE_COLOR if is_arrival else (DEPART_COLOR if is_departure else WHITE)
        row_colors.append((i + 1, bg))

        cam_num   = r['camera_ref'][:3]
        group_col = cam_group_color.get(cam_num) if not is_only_dep else None

        if group_col:
            cam_style = ParagraphStyle('cam_grp', parent=cell_style,
                                       fontName='Helvetica-Bold', textColor=colors.white)
            cam_cell = Paragraph(r['camera_ref'], cam_style)
        else:
            cam_cell = h(r['camera_ref'], bold=True)

        note_text = r['note_soggiorno']
        note_cell = Paragraph(note_text, cell_note) if note_text else h('')

        def fmt_date(d):
            return d[:5] if d and len(d) >= 5 else (d or '')

        row = [cam_cell, note_cell,
               num(r['arrivi']), num(r['casa']), num(r['partenze']),
               num(r['colaz']), num(r['cena']),
               h(r['n']), h(fmt_date(r['arrivo'])), h(fmt_date(r['partenza'])),
               h(r['ad']), h(r['b']),
               h(r['b_0_1'] or ''), h(r['b_2_4'] or ''),
               h(r['b_5_7'] or ''), h(r['b_8_11'] or '')]
        rows_data.append(row)

    # Totals
    def total(field):
        try:
            return str(sum(int(r[field]) for r in merged
                           if r[field] and str(r[field]).isdigit()))
        except:
            return ''

    tot_style = ParagraphStyle('tot', parent=cell_style, fontName='Helvetica-Bold', fontSize=7)
    tot_row = [Paragraph('<b>TOTALE</b>', tot_style), Paragraph('', tot_style),
               Paragraph(f"<b>{total('arrivi')}</b>", tot_style),
               Paragraph(f"<b>{total('casa')}</b>", tot_style),
               Paragraph(f"<b>{total('partenze')}</b>", tot_style),
               Paragraph(f"<b>{total('colaz')}</b>", tot_style),
               Paragraph(f"<b>{total('cena')}</b>", tot_style),
               Paragraph('', tot_style), Paragraph('', tot_style), Paragraph('', tot_style),
               Paragraph(f"<b>{total('ad')}</b>", tot_style),
               Paragraph(f"<b>{total('b')}</b>", tot_style),
               Paragraph('', tot_style), Paragraph('', tot_style),
               Paragraph('', tot_style), Paragraph('', tot_style)]
    rows_data.append(tot_row)

    table = Table(rows_data, colWidths=col_widths, repeatRows=1)

    ts = TableStyle([
        ('BACKGROUND',  (0, 0), (-1, 0), HDR_BG),
        ('TEXTCOLOR',   (0, 0), (-1, 0), HDR_TXT),
        ('FONTNAME',    (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE',    (0, 0), (-1, 0), 7),
        ('ALIGN',       (0, 0), (-1, 0), 'CENTER'),
        ('VALIGN',      (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID',        (0, 0), (-1, -1), 0.3, colors.HexColor('#b0bec5')),
        ('LINEBELOW',   (0, 0), (-1, 0), 1, colors.HexColor('#0d1f3a')),
        ('LINEAFTER',   (6, 0), (6, -1), 1.5, colors.HexColor('#0d1f3a')),
        ('LEFTPADDING', (0, 0), (-1, -1), 2),
        ('RIGHTPADDING',(0, 0), (-1, -1), 2),
        ('TOPPADDING',  (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING',(0,0), (-1, -1), 2),
        ('BACKGROUND',  (0, -1), (-1, -1), colors.HexColor('#e3eaf4')),
        ('LINEABOVE',   (0, -1), (-1, -1), 1, colors.HexColor('#0d1f3a')),
        ('ALIGN',       (2, 1), (-1, -1), 'CENTER'),
        ('ALIGN',       (0, 1), (1, -1), 'LEFT'),
    ])

    for row_idx, bg in row_colors:
        ts.add('BACKGROUND', (0, row_idx), (-1, row_idx), bg)

    for i, r in enumerate(merged):
        cam_num  = r['camera_ref'][:3]
        is_only_dep = (r['partenze'] not in ('', '0') and
                       r['arrivi'] in ('', '0') and r['casa'] in ('', '0'))
        group_col = cam_group_color.get(cam_num) if not is_only_dep else None
        if group_col:
            ts.add('BACKGROUND', (0, i+1), (0, i+1), group_col)

    table.setStyle(ts)

    # Legend
    legend_style = ParagraphStyle('legend', parent=styles['Normal'],
                                  fontSize=6.5, fontName='Helvetica')
    GROUP_HEX = ['#1565c0','#b71c1c','#4a148c','#e65100',
                 '#006064','#33691e','#880e4f','#4e342e']

    story = []
    story.append(Paragraph(f'Lista Sala Giornaliera — Hotel Rosanna', title_style))
    story.append(Paragraph(
        f'Data: {data_str}  |  Generato il {datetime.now().strftime("%d/%m/%Y %H:%M")}  |  Camere: {len(merged)}',
        sub_style))
    story.append(Spacer(1, 2*mm))
    story.append(table)
    story.append(Spacer(1, 3*mm))

    leg_parts = ['<font color="#2e7d32">■</font> Arrivo oggi',
                 '<font color="#e65100">■</font> Partenza oggi']
    if table_groups:
        for g_idx, group in enumerate(table_groups):
            hex_col  = GROUP_HEX[g_idx % len(GROUP_HEX)]
            cams_str = ' + '.join(sorted(group))
            leg_parts.append(f'<font color="{hex_col}">■</font> Tavolo insieme: {cams_str}')
    leg_parts.append('Date come gg/mm  |  Trattino = zero')
    story.append(Paragraph('   '.join(leg_parts), legend_style))

    doc.build(story)
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────
# HTML INTERFACE
# ─────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Lista Sala — Hotel Rosanna</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', Arial, sans-serif; background: #f0f4f8; color: #1a2e4a; min-height: 100vh; }
  header {
    background: #1a2e4a; color: white; padding: 18px 32px;
    display: flex; align-items: center; gap: 16px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.18);
  }
  header h1 { font-size: 1.3rem; font-weight: 700; letter-spacing: 0.5px; }
  header span { font-size: 0.85rem; opacity: 0.7; margin-top: 2px; }
  .logo {
    width: 40px; height: 40px; background: #c8a96e; border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    font-size: 1.4rem; font-weight: 800; color: #1a2e4a;
  }
  main { max-width: 800px; margin: 40px auto; padding: 0 20px; }
  .card {
    background: white; border-radius: 12px;
    box-shadow: 0 2px 12px rgba(26,46,74,0.09);
    padding: 32px; margin-bottom: 24px;
  }
  .card h2 {
    font-size: 1rem; font-weight: 700; color: #1a2e4a;
    margin-bottom: 20px; padding-bottom: 10px; border-bottom: 2px solid #e3eaf4;
  }
  .upload-area { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 24px; }
  .upload-box {
    border: 2px dashed #b0bec5; border-radius: 10px; padding: 24px 16px;
    text-align: center; cursor: pointer; transition: all 0.2s;
    position: relative; background: #f8fafc;
  }
  .upload-box:hover, .upload-box.drag-over { border-color: #1a2e4a; background: #e8eef6; }
  .upload-box.has-file { border-color: #2e7d32; background: #f1f8f2; }
  .upload-box input[type=file] { position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%; }
  .upload-icon { font-size: 2rem; margin-bottom: 8px; }
  .upload-label { font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: #1a2e4a; margin-bottom: 6px; }
  .upload-hint { font-size: 0.75rem; color: #78909c; }
  .upload-filename { font-size: 0.75rem; color: #2e7d32; font-weight: 600; margin-top: 6px; word-break: break-all; }
  .groups-list { display: flex; flex-direction: column; gap: 10px; margin-bottom: 14px; }
  .group-row { display: flex; align-items: center; gap: 10px; }
  .group-badge { width: 18px; height: 18px; border-radius: 4px; flex-shrink: 0; }
  .group-input {
    flex: 1; border: 1.5px solid #cfd8dc; border-radius: 8px;
    padding: 8px 12px; font-size: 0.88rem; color: #1a2e4a;
    font-family: inherit; transition: border-color 0.2s;
  }
  .group-input:focus { outline: none; border-color: #1a2e4a; }
  .group-input::placeholder { color: #90a4ae; }
  .btn-remove { background: none; border: none; cursor: pointer; color: #90a4ae; font-size: 1.1rem; padding: 4px; line-height: 1; transition: color 0.15s; }
  .btn-remove:hover { color: #c62828; }
  .btn-add-group { background: none; border: 1.5px dashed #b0bec5; border-radius: 8px; padding: 8px 16px; color: #546e7a; font-size: 0.82rem; cursor: pointer; transition: all 0.2s; font-family: inherit; }
  .btn-add-group:hover { border-color: #1a2e4a; color: #1a2e4a; }
  .groups-hint { font-size: 0.75rem; color: #90a4ae; margin-top: 8px; }
  .btn-generate {
    width: 100%; background: #1a2e4a; color: white; border: none;
    border-radius: 10px; padding: 16px; font-size: 1rem; font-weight: 700;
    cursor: pointer; letter-spacing: 0.5px; transition: background 0.2s, transform 0.1s;
    display: flex; align-items: center; justify-content: center; gap: 10px; margin-top: 20px;
  }
  .btn-generate:hover:not(:disabled) { background: #243f66; transform: translateY(-1px); }
  .btn-generate:disabled { background: #b0bec5; cursor: not-allowed; transform: none; }
  .legend { display: flex; gap: 20px; flex-wrap: wrap; margin-top: 16px; }
  .legend-item { display: flex; align-items: center; gap: 6px; font-size: 0.78rem; color: #546e7a; }
  .dot { width: 12px; height: 12px; border-radius: 3px; flex-shrink: 0; }
  .status { margin-top: 20px; padding: 12px 16px; border-radius: 8px; font-size: 0.85rem; font-weight: 500; display: none; }
  .status.error { background: #ffebee; color: #c62828; border: 1px solid #ef9a9a; display: block; }
  .status.success { background: #e8f5e9; color: #2e7d32; border: 1px solid #a5d6a7; display: block; }
  .status.loading { background: #e3f2fd; color: #1565c0; border: 1px solid #90caf9; display: block; }
  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid currentColor; border-top-color: transparent; border-radius: 50%; animation: spin 0.7s linear infinite; vertical-align: middle; margin-right: 6px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  footer { text-align: center; font-size: 0.72rem; color: #90a4ae; padding: 20px; }
  @media (max-width: 520px) { .upload-area { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <div class="logo">R</div>
  <div>
    <h1>Lista Sala Giornaliera</h1>
    <span>Hotel Rosanna — Generatore PDF</span>
  </div>
</header>
<main>
  <div class="card">
    <h2>📂 Carica i file del giorno</h2>
    <div class="upload-area">
      <div class="upload-box" id="box1">
        <input type="file" accept=".pdf" id="file1" onchange="fileSelected(1)">
        <div class="upload-icon">📋</div>
        <div class="upload-label">Lista Pasti Giornalieri</div>
        <div class="upload-hint">Trascina qui o clicca</div>
        <div class="upload-filename" id="name1"></div>
      </div>
      <div class="upload-box" id="box2">
        <input type="file" accept=".pdf" id="file2" onchange="fileSelected(2)">
        <div class="upload-icon">🏨</div>
        <div class="upload-label">Arrivi x Sala</div>
        <div class="upload-hint">Trascina qui o clicca</div>
        <div class="upload-filename" id="name2"></div>
      </div>
    </div>
  </div>
  <div class="card">
    <h2>🍽️ Camere allo stesso tavolo</h2>
    <div class="groups-list" id="groupsList"></div>
    <button class="btn-add-group" onclick="addGroup()">+ Aggiungi gruppo tavolo</button>
    <p class="groups-hint">Inserisci i numeri di camera separati da virgola o spazio. Es: <b>203, 204</b> &nbsp;·&nbsp; <b>307 309 311</b></p>
  </div>
  <div class="card">
    <button class="btn-generate" id="btnGen" onclick="genera()" disabled>
      <span>📄</span> Genera Lista Finale PDF
    </button>
    <div class="status" id="status"></div>
  </div>
  <div class="card">
    <h2>📘 Legenda colori PDF</h2>
    <div class="legend">
      <div class="legend-item"><div class="dot" style="background:#c8e6c9"></div> Arrivo oggi</div>
      <div class="legend-item"><div class="dot" style="background:#ffe0b2"></div> Partenza oggi</div>
      <div class="legend-item"><div class="dot" style="background:#1565c0"></div> Tavolo 1 (colore camera)</div>
      <div class="legend-item"><div class="dot" style="background:#b71c1c"></div> Tavolo 2 (colore camera)</div>
      <div class="legend-item"><div class="dot" style="background:#4a148c"></div> Tavolo 3 (colore camera)</div>
    </div>
  </div>
</main>
<footer>Hotel Rosanna &mdash; Uso interno riservato</footer>
<script>
const GROUP_COLORS = ['#1565c0','#b71c1c','#4a148c','#e65100','#006064','#33691e','#880e4f','#4e342e'];
let groupCount = 0;
function addGroup(initialValue) {
  const list = document.getElementById('groupsList');
  const idx = groupCount++;
  const color = GROUP_COLORS[idx % GROUP_COLORS.length];
  const row = document.createElement('div');
  row.className = 'group-row';
  row.dataset.idx = idx;
  row.innerHTML = `
    <div class="group-badge" style="background:${color}"></div>
    <input class="group-input" type="text" placeholder="Es: 203, 204" value="${initialValue || ''}">
    <button class="btn-remove" onclick="removeGroup(this)" title="Rimuovi">✕</button>
  `;
  list.appendChild(row);
}
function removeGroup(btn) { btn.closest('.group-row').remove(); }
function getGroups() {
  const rows = document.querySelectorAll('.group-row');
  const groups = [];
  rows.forEach(row => {
    const val = row.querySelector('input').value.trim();
    if (val) {
      const cams = val.split(/[\s,]+/).filter(Boolean).map(c => c.padStart(3, '0'));
      if (cams.length > 0) groups.push(cams);
    }
  });
  return groups;
}
function fileSelected(n) {
  const f = document.getElementById('file'+n).files[0];
  if (f) {
    document.getElementById('name'+n).textContent = '✓ ' + f.name;
    document.getElementById('box'+n).classList.add('has-file');
  }
  checkReady();
}
function checkReady() {
  const f1 = document.getElementById('file1').files[0];
  const f2 = document.getElementById('file2').files[0];
  document.getElementById('btnGen').disabled = !(f1 && f2);
}
[1, 2].forEach(n => {
  const box = document.getElementById('box'+n);
  box.addEventListener('dragover', e => { e.preventDefault(); box.classList.add('drag-over'); });
  box.addEventListener('dragleave', () => box.classList.remove('drag-over'));
  box.addEventListener('drop', e => {
    e.preventDefault(); box.classList.remove('drag-over');
    const f = e.dataTransfer.files[0];
    if (f && f.name.endsWith('.pdf')) {
      const input = document.getElementById('file'+n);
      const dt = new DataTransfer(); dt.items.add(f); input.files = dt.files;
      fileSelected(n);
    }
  });
});
async function genera() {
  const f1 = document.getElementById('file1').files[0];
  const f2 = document.getElementById('file2').files[0];
  const btn = document.getElementById('btnGen');
  const status = document.getElementById('status');
  btn.disabled = true;
  status.className = 'status loading';
  status.innerHTML = '<span class="spinner"></span> Elaborazione in corso…';
  const fd = new FormData();
  fd.append('lista_pasti', f1);
  fd.append('arrivi_sala', f2);
  fd.append('groups', JSON.stringify(getGroups()));
  try {
    const resp = await fetch('/genera', { method: 'POST', body: fd });
    if (!resp.ok) {
      const err = await resp.json();
      status.className = 'status error';
      status.textContent = '❌ Errore: ' + (err.error || resp.statusText);
      btn.disabled = false;
      return;
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const today = new Date().toLocaleDateString('it-IT').replace(/\//g, '-');
    a.href = url; a.download = `lista_sala_${today}.pdf`; a.click();
    status.className = 'status success';
    status.innerHTML = '<span>✅</span> PDF generato e scaricato! Pronto per la stampa.';
  } catch(e) {
    status.className = 'status error';
    status.textContent = '❌ Errore di rete: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}
</script>
</body>
</html>"""


@app.route('/')
def index():
    return render_template_string(HTML)


@app.route('/genera', methods=['POST'])
def genera():
    try:
        f1 = request.files.get('lista_pasti')
        f2 = request.files.get('arrivi_sala')
        if not f1 or not f2:
            return jsonify({'error': 'Caricare entrambi i file PDF'}), 400

        b1 = f1.read()
        b2 = f2.read()

        pasti, pdf_date = parse_lista_pasti(b1)
        arrivi          = parse_arrivi_sala(b2)

        if not pasti:
            return jsonify({'error': 'Impossibile leggere "Lista Pasti Giornalieri".'}), 400
        if not arrivi:
            return jsonify({'error': 'Impossibile leggere "Arrivi x Sala".'}), 400

        merged = merge_data(pasti, arrivi)

        import json as _json
        groups_raw = request.form.get('groups', '[]')
        try:
            table_groups = _json.loads(groups_raw)
        except Exception:
            table_groups = []

        data_str  = pdf_date if pdf_date else datetime.now().strftime('%d/%m/%Y')
        file_date = data_str.replace('/', '') if pdf_date else datetime.now().strftime('%Y%m%d')
        pdf_buf   = generate_pdf(merged, data_str, table_groups=table_groups)

        return send_file(pdf_buf, mimetype='application/pdf',
                         as_attachment=True,
                         download_name=f'lista_sala_{file_date}.pdf')

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    app.run(host='0.0.0.0', port=port, debug=False)
