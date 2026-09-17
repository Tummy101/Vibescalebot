import sqlite3
from datetime import datetime

DATABASE_NAME = "vibescale.db"


def get_connection():
    return sqlite3.connect(DATABASE_NAME)


def initialize_database():
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT UNIQUE NOT NULL,
            telegram_user_id INTEGER NOT NULL,
            telegram_username TEXT,
            plan TEXT NOT NULL,
            x_username TEXT NOT NULL,
            duration_days INTEGER NOT NULL,
            total_price REAL NOT NULL,
            payment_status TEXT NOT NULL DEFAULT 'pending',
            order_status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        )
    """)

    # Add payment-verification fields to an existing database without deleting it.
    cursor.execute("PRAGMA table_info(orders)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    if "payment_network" not in existing_columns:
        cursor.execute(
            "ALTER TABLE orders ADD COLUMN payment_network TEXT"
        )

    if "transaction_hash" not in existing_columns:
        cursor.execute(
            "ALTER TABLE orders ADD COLUMN transaction_hash TEXT"
        )

    if "customer_email" not in existing_columns:
        cursor.execute(
            "ALTER TABLE orders ADD COLUMN customer_email TEXT"
        )

    if "paystack_reference" not in existing_columns:
        cursor.execute(
            "ALTER TABLE orders ADD COLUMN paystack_reference TEXT"
        )

    if "paystack_ngn_amount" not in existing_columns:
        cursor.execute(
            "ALTER TABLE orders ADD COLUMN paystack_ngn_amount INTEGER"
        )

    if "fx_rate_usd_ngn" not in existing_columns:
        cursor.execute(
            "ALTER TABLE orders ADD COLUMN fx_rate_usd_ngn REAL"
        )

    if "fx_source" not in existing_columns:
        cursor.execute(
            "ALTER TABLE orders ADD COLUMN fx_source TEXT"
        )

    if "activated_at" not in existing_columns:
        cursor.execute(
            "ALTER TABLE orders ADD COLUMN activated_at TEXT"
        )

    # Custom-plan management fields.
    cursor.execute("PRAGMA table_info(custom_quotes)")
    custom_columns = {row[1] for row in cursor.fetchall()}
    if "quoted_price" not in custom_columns:
        cursor.execute("ALTER TABLE custom_quotes ADD COLUMN quoted_price REAL")
    if "admin_note" not in custom_columns:
        cursor.execute("ALTER TABLE custom_quotes ADD COLUMN admin_note TEXT")
    if "updated_at" not in custom_columns:
        cursor.execute("ALTER TABLE custom_quotes ADD COLUMN updated_at TEXT")

    connection.commit()
    connection.close()
    ensure_campaign_table()


def create_order(
    order_id,
    telegram_user_id,
    telegram_username,
    plan,
    x_username,
    duration_days,
    total_price
):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        INSERT INTO orders (
            order_id,
            telegram_user_id,
            telegram_username,
            plan,
            x_username,
            duration_days,
            total_price,
            payment_status,
            order_status,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        order_id,
        telegram_user_id,
        telegram_username,
        plan,
        x_username,
        duration_days,
        total_price,
        "pending",
        "pending",
        datetime.now().isoformat()
    ))
    connection.commit()
    connection.close()


def save_payment_submission(order_id, payment_network, transaction_hash):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        UPDATE orders
        SET payment_network = ?,
            transaction_hash = ?,
            payment_status = 'pending'
        WHERE order_id = ?
    """, (
        payment_network,
        transaction_hash,
        order_id,
    ))

    connection.commit()
    connection.close()


def save_paystack_reference(order_id, customer_email, paystack_reference):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        UPDATE orders
        SET customer_email = ?,
            paystack_reference = ?,
            payment_network = 'Paystack'
        WHERE order_id = ?
    """, (
        customer_email,
        paystack_reference,
        order_id,
    ))
    connection.commit()
    connection.close()


def get_order_by_id(order_id):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        SELECT order_id, telegram_user_id, telegram_username, plan,
               x_username, duration_days, total_price, payment_status,
               order_status, payment_network, transaction_hash, created_at,
               customer_email, paystack_reference, paystack_ngn_amount,
               fx_rate_usd_ngn, fx_source, activated_at
        FROM orders
        WHERE order_id = ?
    """, (order_id,))
    order = cursor.fetchone()
    connection.close()
    return order


def update_payment_status(order_id, payment_status, order_status):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        UPDATE orders
        SET payment_status = ?,
            order_status = ?,
            activated_at = CASE
                WHEN ? = 'active' THEN COALESCE(activated_at, ?)
                ELSE activated_at
            END
        WHERE order_id = ?
    """, (
        payment_status,
        order_status,
        order_status,
        datetime.now().isoformat(),
        order_id,
    ))
    changed = cursor.rowcount
    connection.commit()
    connection.close()
    return changed


def get_user_orders(telegram_user_id):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        SELECT
            order_id,
            plan,
            x_username,
            duration_days,
            total_price,
            payment_status,
            order_status,
            created_at,
            activated_at
        FROM orders
        WHERE telegram_user_id = ?
        ORDER BY id DESC
    """, (telegram_user_id,))
    orders = cursor.fetchall()
    connection.close()
    return orders


def save_paystack_fx(order_id, ngn_amount, fx_rate_usd_ngn, fx_source):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        UPDATE orders
        SET paystack_ngn_amount = ?,
            fx_rate_usd_ngn = ?,
            fx_source = ?
        WHERE order_id = ?
    """, (
        int(ngn_amount),
        float(fx_rate_usd_ngn),
        fx_source,
        order_id,
    ))
    connection.commit()
    connection.close()



def ensure_campaign_table():
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS campaign_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT UNIQUE NOT NULL,
            target_posts INTEGER NOT NULL DEFAULT 0,
            target_likes INTEGER NOT NULL DEFAULT 0,
            target_reposts INTEGER NOT NULL DEFAULT 0,
            target_bookmarks INTEGER NOT NULL DEFAULT 0,
            target_views INTEGER NOT NULL DEFAULT 0,
            target_comments INTEGER NOT NULL DEFAULT 0,
            delivered_posts INTEGER NOT NULL DEFAULT 0,
            delivered_likes INTEGER NOT NULL DEFAULT 0,
            delivered_reposts INTEGER NOT NULL DEFAULT 0,
            delivered_bookmarks INTEGER NOT NULL DEFAULT 0,
            delivered_views INTEGER NOT NULL DEFAULT 0,
            delivered_comments INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    connection.commit()
    connection.close()


def create_or_update_campaign(order_id, targets):
    ensure_campaign_table()
    connection = get_connection()
    cursor = connection.cursor()
    now = datetime.now().isoformat()
    cursor.execute("""
        INSERT INTO campaign_metrics (
            order_id, target_posts, target_likes, target_reposts, target_bookmarks,
            target_views, target_comments, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(order_id) DO UPDATE SET
            target_posts=excluded.target_posts, target_likes=excluded.target_likes,
            target_reposts=excluded.target_reposts, target_bookmarks=excluded.target_bookmarks,
            target_views=excluded.target_views, target_comments=excluded.target_comments,
            updated_at=excluded.updated_at
    """, (order_id, int(targets.get('posts',0)), int(targets.get('likes',0)),
          int(targets.get('reposts',0)), int(targets.get('bookmarks',0)),
          int(targets.get('views',0)), int(targets.get('comments',0)), now, now))
    connection.commit()
    connection.close()


def get_campaign_metrics(order_id):
    ensure_campaign_table()
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        SELECT order_id, target_posts, target_likes, target_reposts, target_bookmarks,
               target_views, target_comments, delivered_posts, delivered_likes,
               delivered_reposts, delivered_bookmarks, delivered_views, delivered_comments,
               created_at, updated_at
        FROM campaign_metrics WHERE order_id = ?
    """, (order_id,))
    row = cursor.fetchone()
    connection.close()
    return row


def update_campaign_metrics(order_id, delivered):
    ensure_campaign_table()
    connection = get_connection()
    cursor = connection.cursor()
    fields = []
    values = []
    allowed = ['posts','likes','reposts','bookmarks','views','comments']
    for key in allowed:
        if key in delivered:
            fields.append(f"delivered_{key} = ?")
            values.append(max(0, int(delivered[key])))
    if not fields:
        connection.close()
        return 0
    fields.append("updated_at = ?")
    values.append(datetime.now().isoformat())
    values.append(order_id)
    cursor.execute(f"UPDATE campaign_metrics SET {', '.join(fields)} WHERE order_id = ?", values)
    changed = cursor.rowcount
    connection.commit()
    connection.close()
    return changed


def get_admin_campaigns(limit=30):
    ensure_campaign_table()
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        SELECT o.order_id, o.telegram_user_id, o.telegram_username, o.plan, o.x_username,
               o.duration_days, o.total_price, o.payment_status, o.order_status,
               o.created_at, o.activated_at,
               c.target_posts, c.target_likes, c.target_reposts, c.target_bookmarks,
               c.target_views, c.target_comments, c.delivered_posts, c.delivered_likes,
               c.delivered_reposts, c.delivered_bookmarks, c.delivered_views, c.delivered_comments
        FROM orders o JOIN campaign_metrics c ON c.order_id = o.order_id
        WHERE o.payment_status = 'paid'
        ORDER BY o.id DESC LIMIT ?
    """, (int(limit),))
    rows = cursor.fetchall()
    connection.close()
    return rows

def create_support_ticket(ticket_id, telegram_user_id, telegram_username, subject, message):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS support_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT UNIQUE NOT NULL,
            telegram_user_id INTEGER NOT NULL,
            telegram_username TEXT,
            subject TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    now = datetime.now().isoformat()
    cursor.execute("""
        INSERT INTO support_tickets (
            ticket_id, telegram_user_id, telegram_username,
            subject, message, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
    """, (
        ticket_id, telegram_user_id, telegram_username,
        subject, message, now, now
    ))
    connection.commit()
    connection.close()


def get_user_support_tickets(telegram_user_id):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        SELECT ticket_id, subject, message, status, created_at, updated_at
        FROM support_tickets
        WHERE telegram_user_id = ?
        ORDER BY id DESC
    """, (telegram_user_id,))
    rows = cursor.fetchall()
    connection.close()
    return rows


def get_support_ticket(ticket_id):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        SELECT ticket_id, telegram_user_id, telegram_username,
               subject, message, status, created_at, updated_at
        FROM support_tickets
        WHERE ticket_id = ?
    """, (ticket_id,))
    row = cursor.fetchone()
    connection.close()
    return row


def get_admin_support_tickets(limit=20):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        SELECT ticket_id, telegram_user_id, telegram_username,
               subject, message, status, created_at, updated_at
        FROM support_tickets
        ORDER BY id DESC
        LIMIT ?
    """, (int(limit),))
    rows = cursor.fetchall()
    connection.close()
    return rows


def update_support_ticket_status(ticket_id, status):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        UPDATE support_tickets
        SET status = ?, updated_at = ?
        WHERE ticket_id = ?
    """, (status, datetime.now().isoformat(), ticket_id))
    changed = cursor.rowcount
    connection.commit()
    connection.close()
    return changed


def get_admin_order_stats():
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("SELECT COUNT(*) FROM orders")
    total_orders = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM orders WHERE payment_status = 'paid'")
    paid_orders = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM orders WHERE order_status = 'active'")
    active_orders = cursor.fetchone()[0]

    cursor.execute("SELECT COALESCE(SUM(total_price), 0) FROM orders WHERE payment_status = 'paid'")
    paid_value = cursor.fetchone()[0] or 0

    cursor.execute("""
        SELECT order_id, telegram_username, plan, x_username,
               total_price, payment_status, order_status, created_at
        FROM orders
        ORDER BY id DESC
        LIMIT 10
    """)
    recent_orders = cursor.fetchall()

    connection.close()
    return {
        "total_orders": total_orders,
        "paid_orders": paid_orders,
        "active_orders": active_orders,
        "paid_value": paid_value,
        "recent_orders": recent_orders,
    }


def get_admin_custom_quotes(limit=10):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        SELECT request_id, telegram_user_id, telegram_username,
               x_username, posts, likes, reposts, bookmarks, views,
               comments, duration_days, notes, status, created_at,
               quoted_price, admin_note, updated_at
        FROM custom_quotes
        ORDER BY id DESC
        LIMIT ?
    """, (int(limit),))
    rows = cursor.fetchall()
    connection.close()
    return rows


def get_custom_quote(request_id):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        SELECT request_id, telegram_user_id, telegram_username,
               x_username, posts, likes, reposts, bookmarks, views,
               comments, duration_days, notes, status, created_at,
               quoted_price, admin_note, updated_at
        FROM custom_quotes
        WHERE request_id = ?
    """, (request_id,))
    row = cursor.fetchone()
    connection.close()
    return row


def update_custom_quote(request_id, status=None, quoted_price=None, admin_note=None):
    connection = get_connection()
    cursor = connection.cursor()

    fields = []
    values = []
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if quoted_price is not None:
        fields.append("quoted_price = ?")
        values.append(float(quoted_price))
    if admin_note is not None:
        fields.append("admin_note = ?")
        values.append(admin_note)

    if not fields:
        connection.close()
        return 0

    fields.append("updated_at = ?")
    values.append(datetime.now().isoformat())
    values.append(request_id)

    cursor.execute(
        f"UPDATE custom_quotes SET {', '.join(fields)} WHERE request_id = ?",
        values,
    )
    changed = cursor.rowcount
    connection.commit()
    connection.close()
    return changed


def cancel_custom_quote(request_id, telegram_user_id):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        UPDATE custom_quotes
        SET status = 'cancelled', updated_at = ?
        WHERE request_id = ?
          AND telegram_user_id = ?
          AND status IN ('awaiting_quote', 'quoted')
    """, (datetime.now().isoformat(), request_id, telegram_user_id))
    changed = cursor.rowcount
    connection.commit()
    connection.close()
    return changed



def create_custom_quote(
    request_id, telegram_user_id, telegram_username, x_username,
    posts, likes, reposts, bookmarks, views, comments, duration_days, notes
):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS custom_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT UNIQUE NOT NULL,
            telegram_user_id INTEGER NOT NULL,
            telegram_username TEXT,
            x_username TEXT NOT NULL,
            posts INTEGER NOT NULL,
            likes INTEGER NOT NULL,
            reposts INTEGER NOT NULL,
            bookmarks INTEGER NOT NULL,
            views INTEGER NOT NULL,
            comments INTEGER NOT NULL,
            duration_days INTEGER NOT NULL,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'awaiting_quote',
            created_at TEXT NOT NULL
        )
    """)
    cursor.execute("""
        INSERT INTO custom_quotes (
            request_id, telegram_user_id, telegram_username, x_username,
            posts, likes, reposts, bookmarks, views, comments, duration_days,
            notes, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        request_id, telegram_user_id, telegram_username, x_username,
        int(posts), int(likes), int(reposts), int(bookmarks), int(views), int(comments),
        int(duration_days), notes, 'awaiting_quote', datetime.now().isoformat()
    ))
    connection.commit()
    connection.close()


def get_user_custom_quotes(telegram_user_id):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        SELECT request_id, x_username, duration_days, status, created_at
        FROM custom_quotes
        WHERE telegram_user_id = ?
        ORDER BY id DESC
    """, (telegram_user_id,))
    rows = cursor.fetchall()
    connection.close()
    return rows
