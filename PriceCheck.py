"""
app.py — Sukses Crown Toys · Cek Harga
======================================
Source data barang: [dbo].[v_m_barang_satuan]
  Kolom: kd_barang, barang, satuan, jumlah, harga_jual

Keamanan:
  - Parameterized query (?) untuk semua input user
  - Whitelist karakter kode barang
  - Escape wildcard LIKE
  - Error detail hanya di server log, bukan ke client
  - DEBUG = False

Kompatibilitas:
  - ROW_NUMBER() — SQL Server 2005+
"""

from flask import Flask, render_template, jsonify, request
import pyodbc
import os
import re
import sys
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

DB_CONFIG = {
    'server':   os.getenv('DB_SERVER'),
    'database': os.getenv('DB_DATABASE', 'SOLID_SIM'),
    'username': os.getenv('DB_USERNAME'),
    'password': os.getenv('DB_PASSWORD'),
    'driver':   os.getenv('DB_DRIVER', 'ODBC Driver 17 for SQL Server'),
}

_missing = [k for k in ['server', 'username', 'password'] if not DB_CONFIG.get(k)]
if _missing:
    logger.error("Konfigurasi .env belum lengkap: %s", ', '.join(k.upper() for k in _missing))
    sys.exit(1)


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_db_connection():
    conn_str = (
        f"DRIVER={{{DB_CONFIG['driver']}}};"
        f"SERVER={DB_CONFIG['server']};"
        f"DATABASE={DB_CONFIG['database']};"
        f"UID={DB_CONFIG['username']};"
        f"PWD={DB_CONFIG['password']};"
        "TrustServerCertificate=yes;"
        "Connection Timeout=10;"
    )
    return pyodbc.connect(conn_str)


def _safe_kode(raw):
    """Whitelist kode barang — A-Z 0-9 - . _ / spasi, max 50 char."""
    if not raw:
        return None
    cleaned = raw.strip().upper()
    if not re.match(r'^[A-Z0-9\-\._/ ]{1,50}$', cleaned):
        return None
    return cleaned


def _safe_int(value, default, min_val, max_val):
    try:
        return max(min_val, min(max_val, int(value)))
    except (TypeError, ValueError):
        return default


def _safe_search(raw, max_len=100):
    """Escape wildcard SQL, kembalikan string bersih untuk LIKE prefix."""
    if not raw:
        return ''
    s = raw.strip()[:max_len]
    s = s.replace('[', '[[]').replace('%', '[%]').replace('_', '[_]')
    return s


def _mask_phone(phone):
    if not phone:
        return ''
    digits = ''.join(c for c in phone if c.isdigit())
    if len(digits) <= 4:
        return phone
    return '*' * (len(digits) - 4) + digits[-4:]


def _row_to_satuan(r):
    """Konversi baris DB v_m_barang_satuan ke dict."""
    return {
        'kd_barang':  r[0] or '',
        'barang':     r[1] or '',
        'satuan':     r[2] or '',
        'jumlah':     int(r[3]) if r[3] is not None else 1,
        'harga_jual': float(r[4]) if r[4] is not None else 0.0,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


# ── API: Cek Harga by Kode — kembalikan SEMUA satuan barang tsb ──────────────

@app.route('/api/cek-harga/<path:kode_raw>')
def cek_harga(kode_raw):
    """
    Mengembalikan semua baris satuan untuk kd_barang yang ditemukan.
    Response:
      data.kd_barang, data.barang  — info utama
      data.satuan_list             — list [{satuan, jumlah, harga_jual}, ...]
      data.default                 — satuan dengan jumlah terkecil (satuan terkecil)
    """
    kode = _safe_kode(kode_raw)
    if not kode:
        return jsonify({'success': False, 'message': 'Kode tidak valid'}), 400

    conn = None
    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT [kd_barang], [barang], [satuan], [jumlah], [harga_jual]
            FROM   [dbo].[v_m_barang_satuan]
            WHERE  [kd_barang] = ?
              AND  [status] <> 0
            ORDER  BY [jumlah] ASC
            """,
            (kode,)
        )
        rows = cursor.fetchall()

        if not rows:
            return jsonify({
                'success': False,
                'message': 'Barang dengan kode "{}" tidak ditemukan'.format(kode),
            }), 404

        satuan_list = [_row_to_satuan(r) for r in rows]
        # default = satuan dengan jumlah terkecil (biasanya PCS)
        default = satuan_list[0]

        return jsonify({
            'success': True,
            'data': {
                'kd_barang':   default['kd_barang'],
                'barang':      default['barang'],
                'satuan_list': satuan_list,
                'default':     default,
            }
        })

    except pyodbc.Error:
        logger.exception("DB error on cek_harga kode=%s", kode)
        return jsonify({'success': False, 'message': 'Terjadi kesalahan database'}), 500
    except Exception:
        logger.exception("Unexpected error on cek_harga kode=%s", kode)
        return jsonify({'success': False, 'message': 'Terjadi kesalahan server'}), 500
    finally:
        if conn:
            try: conn.close()
            except: pass


# ── API: List / Search Barang ─────────────────────────────────────────────────

@app.route('/api/barang')
def list_barang():
    """
    Source: v_m_barang_satuan
    Tampilkan 1 baris per kd_barang (satuan terkecil / jumlah terkecil).
    Query params:
      q     — prefix search nama/kode (min 2 char)
      page, limit
    """
    raw_q = request.args.get('q', '')
    page  = _safe_int(request.args.get('page',  1),  default=1,  min_val=1,  max_val=9999)
    limit = _safe_int(request.args.get('limit', 30), default=30, min_val=10, max_val=100)

    q = _safe_search(raw_q)

    if len(q) < 2:
        return jsonify({
            'success': True, 'total': 0, 'page': 1, 'limit': limit, 'pages': 1,
            'data': [], 'require_search': True,
            'message': 'Ketik minimal 2 karakter untuk mencari barang',
        })

    conn = None
    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        like_q = '{}%'.format(q)

        # COUNT unique kd_barang
        cursor.execute(
            """
            SELECT COUNT(DISTINCT [kd_barang])
            FROM   [dbo].[v_m_barang_satuan]
            WHERE  [status] <> 0
              AND  ([barang] LIKE ? ESCAPE '[' OR [kd_barang] LIKE ? ESCAPE '[')
            """,
            (like_q, like_q)
        )
        total = cursor.fetchone()[0]
        pages = max(1, (total + limit - 1) // limit)
        page  = min(page, pages)
        rs    = (page - 1) * limit + 1
        re_   = page * limit

        # Ambil 1 baris per kd_barang (satuan terkecil = jumlah MIN)
        # lalu paginasi dengan ROW_NUMBER
        data_query = """
            SELECT [kd_barang], [barang], [satuan], [jumlah], [harga_jual]
            FROM (
                SELECT
                    [kd_barang], [barang], [satuan], [jumlah], [harga_jual],
                    ROW_NUMBER() OVER (ORDER BY [barang], [kd_barang]) AS _rn
                FROM (
                    SELECT
                        [kd_barang], [barang], [satuan], [jumlah], [harga_jual],
                        ROW_NUMBER() OVER (
                            PARTITION BY [kd_barang]
                            ORDER BY [jumlah] ASC
                        ) AS _dr
                    FROM [dbo].[v_m_barang_satuan]
                    WHERE [status] <> 0
                      AND ([barang] LIKE ? ESCAPE '[' OR [kd_barang] LIKE ? ESCAPE '[')
                ) AS _d
                WHERE _dr = 1
            ) AS _p
            WHERE _rn BETWEEN {rs} AND {re}
            ORDER BY _rn
        """.format(rs=rs, re=re_)

        cursor.execute(data_query, (like_q, like_q))
        rows = cursor.fetchall()

        return jsonify({
            'success': True,
            'total':   total,
            'page':    page,
            'limit':   limit,
            'pages':   pages,
            'data':    [_row_to_satuan(r) for r in rows],
        })

    except pyodbc.Error:
        logger.exception("DB error on list_barang")
        return jsonify({'success': False, 'message': 'Terjadi kesalahan database'}), 500
    except Exception:
        logger.exception("Unexpected error on list_barang")
        return jsonify({'success': False, 'message': 'Terjadi kesalahan server'}), 500
    finally:
        if conn:
            try: conn.close()
            except: pass


# ── API: List / Search Member ─────────────────────────────────────────────────

@app.route('/api/member')
def list_member():
    raw_q = request.args.get('q', '')
    page  = _safe_int(request.args.get('page',  1),  default=1,  min_val=1,  max_val=9999)
    limit = _safe_int(request.args.get('limit', 30), default=30, min_val=10, max_val=100)

    q = _safe_search(raw_q)

    if len(q) < 2:
        return jsonify({
            'success': True, 'total': 0, 'page': 1, 'limit': limit, 'pages': 1,
            'data': [], 'require_search': True,
            'message': 'Ketik minimal 2 karakter nama / kota / kode member',
        })

    conn = None
    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        like_q = '{}%'.format(q)
        where  = (
            "WHERE [Status] = 1 AND "
            "([Customer] LIKE ? ESCAPE '[' OR [Kota] LIKE ? ESCAPE '[' OR [Kode] LIKE ? ESCAPE '[')"
        )
        params = [like_q, like_q, like_q]

        cursor.execute("SELECT COUNT(*) FROM [dbo].[mon_m_customer] " + where, params)
        total = cursor.fetchone()[0]
        pages = max(1, (total + limit - 1) // limit)
        page  = min(page, pages)
        rs    = (page - 1) * limit + 1
        re_   = page * limit

        data_query = """
            SELECT * FROM (
                SELECT
                    [Kode], [Customer], [Kota], [Alamat],
                    [Telepon], [Fax], [HP], [Point], [Disc], [Status], [Keterangan],
                    ROW_NUMBER() OVER (ORDER BY [Customer]) AS _rn
                FROM [dbo].[mon_m_customer]
                {where}
            ) AS _p
            WHERE _rn BETWEEN {rs} AND {re}
            ORDER BY _rn
        """.format(where=where, rs=rs, re=re_)

        cursor.execute(data_query, params)
        rows = cursor.fetchall()

        return jsonify({
            'success': True,
            'total':   total,
            'page':    page,
            'limit':   limit,
            'pages':   pages,
            'data': [
                {
                    'kode':       r[0] or '',
                    'customer':   r[1] or '',
                    'kota':       r[2] or '',
                    'alamat':     r[3] or '',
                    'telepon':    _mask_phone(r[4] or ''),
                    'fax':        _mask_phone(r[5] or ''),
                    'hp':         _mask_phone(r[6] or ''),
                    'point':      float(r[7]) if r[7] is not None else 0.0,
                    'disc':       float(r[8]) if r[8] is not None else 0.0,
                    'status':     r[9],
                    'keterangan': r[10] or '',
                }
                for r in rows
            ]
        })

    except pyodbc.Error:
        logger.exception("DB error on list_member")
        return jsonify({'success': False, 'message': 'Terjadi kesalahan database'}), 500
    except Exception:
        logger.exception("Unexpected error on list_member")
        return jsonify({'success': False, 'message': 'Terjadi kesalahan server'}), 500
    finally:
        if conn:
            try: conn.close()
            except: pass


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = _safe_int(os.getenv('FLASK_PORT', 5000), default=5000, min_val=1024, max_val=65535)
    logger.info("Starting Sukses Crown Toys server on port %d", port)
    app.run(host='0.0.0.0', port=port, debug=False)