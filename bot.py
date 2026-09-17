import os
import random
import re

import httpx

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TimedOut, TelegramError
from datetime import datetime

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import (
    initialize_database,
    create_order,
    get_user_orders,
    save_payment_submission,
    get_order_by_id,
    update_payment_status,
    save_paystack_reference,
    save_paystack_fx,
    create_custom_quote,
    get_user_custom_quotes,
    create_support_ticket,
    get_user_support_tickets,
    get_support_ticket,
    get_admin_support_tickets,
    update_support_ticket_status,
    get_admin_order_stats,
    get_admin_custom_quotes,
    get_custom_quote,
    update_custom_quote,
    cancel_custom_quote,
    create_or_update_campaign,
    get_campaign_metrics,
    update_campaign_metrics,
    get_admin_campaigns,
)

# =========================
# VibeScale Crypto Payments
# =========================

CRYPTO_ADDRESSES = {
    "USDT_TON": {
        "name": "USDT — TON",
        "network": "TON",
        "address": "UQDHGPFOQaiTE6eW4I3-wzW9dBhqce-6TP_p2wt6SjKuEPDk",
    },
    "USDT_SOLANA": {
        "name": "USDT — Solana",
        "network": "Solana",
        "address": "6B5gfeeGLncfHEeWYryQZkvKX8DR9ABVpvZxvEmvMWz9",
    },
    "USDT_TRC20": {
        "name": "USDT — TRC20",
        "network": "TRON (TRC20)",
        "address": "TRokxAz3fcXfL118EmYsHAhA3XCcDbVV8",
    },
    "USDT_ERC20": {
        "name": "USDT — ERC20",
        "network": "Ethereum (ERC20)",
        "address": "0xaf2A0AF6fcB1439e1d7f82c6A229d33e2E3c39A3",
    },
    "USDT_BEP20": {
        "name": "USDT — BEP20",
        "network": "BNB Smart Chain (BEP20)",
        "address": "0xaf2A0AF6fcB1439e1d7f82c6A229d33e2E3c39A3",
    },
}


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
PAYSTACK_CURRENCY = "NGN"
CURRENCYFREAKS_API_KEY = os.getenv("CURRENCYFREAKS_API_KEY")
FX_CACHE_SECONDS = int(os.getenv("FX_CACHE_SECONDS", "300"))
ADMIN_USER_ID = 7259608394
PAYSTACK_API_URL = "https://api.paystack.co"


# -------------------------
# TELEGRAM SAFE CALLBACK/EDIT HELPERS
# -------------------------

async def safe_answer_callback(query, text=None, show_alert=False):
    """Answer callback queries without crashing on transient Telegram timeouts."""
    try:
        await query.answer(text=text, show_alert=show_alert, read_timeout=10, write_timeout=10, connect_timeout=10, pool_timeout=10)
        return True
    except TimedOut:
        print("[TELEGRAM] Callback answer timed out; continuing.")
        return False
    except TelegramError as exc:
        print(f"[TELEGRAM] Callback answer failed: {exc}")
        return False


async def safe_edit_message_text(query, text, **kwargs):
    """Edit a bot message, ignoring Telegram's harmless 'message is not modified'."""
    try:
        return await query.edit_message_text(
            text=text,
            read_timeout=10,
            write_timeout=10,
            connect_timeout=10,
            pool_timeout=10,
            **kwargs,
        )
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            print("[TELEGRAM] Ignored harmless 'Message is not modified'.")
            return None
        raise


async def telegram_error_handler(update, context):
    """Keep polling alive and log unexpected Telegram/API errors."""
    print(f"[TELEGRAM ERROR] {type(context.error).__name__}: {context.error}")


# -------------------------
# PAYSTACK HELPERS
# -------------------------

async def initialize_paystack_transaction(order_id, amount, email, plan, x_username, ngn_amount):
    if not PAYSTACK_SECRET_KEY:
        return None, "Paystack is not configured on the server yet."

    # Paystack expects NGN amounts in kobo.
    amount_subunit = int(ngn_amount) * 100
    reference = f"VSB-{order_id}-{random.randint(1000, 9999)}"

    channels = ["card", "bank_transfer", "ussd"]

    payload = {
        "amount": amount_subunit,
        "email": email,
        "currency": PAYSTACK_CURRENCY,
        "reference": reference,
        "channels": channels,
        "metadata": {
            "order_id": order_id,
            "plan": plan,
            "x_username": x_username,
            "usd_amount": f"{float(amount):.2f}",
            "ngn_amount": str(int(ngn_amount)),
        },
    }

    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{PAYSTACK_API_URL}/transaction/initialize",
                json=payload,
                headers=headers,
            )
        data = response.json()
    except Exception as exc:
        print(f"Paystack initialization error: {exc}")
        return None, "Could not connect to Paystack. Please try again."

    if response.status_code >= 400 or not data.get("status"):
        print(f"Paystack initialization failed: {response.status_code} {data}")
        message = data.get("message") or "Paystack could not initialize this payment."
        return None, message

    payment_data = data.get("data") or {}
    authorization_url = payment_data.get("authorization_url")
    paystack_reference = payment_data.get("reference") or reference

    if not authorization_url:
        return None, "Paystack did not return a checkout URL."

    save_paystack_reference(order_id, email, paystack_reference)
    return {
        "authorization_url": authorization_url,
        "reference": paystack_reference,
    }, None


async def verify_paystack_transaction(reference):
    if not PAYSTACK_SECRET_KEY:
        return None, "Paystack is not configured on the server yet."

    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{PAYSTACK_API_URL}/transaction/verify/{reference}",
                headers=headers,
            )
        data = response.json()
    except Exception as exc:
        print(f"Paystack verification error: {exc}")
        return None, "Could not connect to Paystack for verification."

    if response.status_code >= 400 or not data.get("status"):
        print(f"Paystack verification failed: {response.status_code} {data}")
        return None, data.get("message") or "Paystack verification failed."

    return data.get("data"), None


async def get_usd_ngn_rate():
    """Fetch the latest USD->NGN rate from CurrencyFreaks."""
    if not CURRENCYFREAKS_API_KEY:
        return None, "Currency conversion is not configured on the server yet."

    url = "https://api.currencyfreaks.com/v2.0/rates/latest"
    params = {
        "apikey": CURRENCYFREAKS_API_KEY,
        "symbols": "NGN",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, params=params)
        data = response.json()
    except Exception as exc:
        print(f"FX rate error: {exc}")
        return None, "Could not retrieve the latest USD/NGN exchange rate."

    if response.status_code >= 400:
        print(f"FX rate failed: {response.status_code} {data}")
        return None, data.get("message") or "Could not retrieve the latest USD/NGN exchange rate."

    try:
        rate = float((data.get("rates") or {}).get("NGN"))
    except (TypeError, ValueError):
        rate = None

    if not rate or rate <= 0:
        return None, "The exchange-rate provider returned an invalid USD/NGN rate."

    return rate, None


async def calculate_ngn_amount(usd_amount):
    rate, error = await get_usd_ngn_rate()
    if error:
        return None, None, error

    # Paystack requires NGN in kobo. We round to the nearest whole naira
    # before converting to kobo so the customer sees a clean NGN amount.
    ngn_amount = int(round(float(usd_amount) * rate))
    return ngn_amount, rate, None


def valid_email(email):
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email.strip()))


# -------------------------
# MAIN MENU
# -------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("📦 Plans", callback_data="plans"),
            InlineKeyboardButton("🛒 Create Order", callback_data="order"),
        ],
        [
            InlineKeyboardButton("🧩 Custom Plan", callback_data="custom"),
            InlineKeyboardButton("📊 My Campaign", callback_data="campaign"),
        ],
        [
            InlineKeyboardButton("📋 My Orders", callback_data="orders"),
            InlineKeyboardButton("💳 Payments & Wallet", callback_data="payment"),
        ],
        [
            InlineKeyboardButton("🆘 Support", callback_data="support"),
            InlineKeyboardButton("ℹ️ How It Works", callback_data="help"),
        ],
    ]

    await update.message.reply_text(
        "🚀 Welcome to VibeScale!\n\n"
        "X/Twitter content amplification made simple.\n\n"
        "Choose an option below:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# -------------------------
# BUTTON HANDLER
# -------------------------

async def notify_customer_payment_result(context, telegram_user_id, order_id, approved):
    if approved:
        message = (
            "✅ PAYMENT VERIFIED\n\n"
            f"🆔 Order: {order_id}\n\n"
            "Your payment has been verified successfully.\n"
            "Your VibeScale order is now active and ready for campaign processing."
        )
    else:
        message = (
            "❌ PAYMENT NOT VERIFIED\n\n"
            f"🆔 Order: {order_id}\n\n"
            "We could not verify the submitted payment at this time.\n"
            "Please check your transaction details and contact VibeScale Support "
            "if you believe this was an error."
        )

    await context.bot.send_message(chat_id=telegram_user_id, text=message)


async def paystack_verify_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the Paystack 'I've Completed Payment' button explicitly."""
    query = update.callback_query
    await safe_answer_callback(query, "Checking payment...", show_alert=False)

    order_id = query.data.replace("paystack_verify_", "", 1)
    print(f"[PAYSTACK VERIFY CLICK] order_id={order_id} user={update.effective_user.id}")

    order = get_order_by_id(order_id)
    if not order:
        await safe_edit_message_text(query, 
            "❌ ORDER NOT FOUND\n\n"
            "This payment session could not be matched to an order.\n"
            "Please use /start to create a new order."
        )
        return

    (
        db_order_id, telegram_user_id, telegram_username, plan,
        x_username, duration_days, total_price, payment_status,
        order_status, payment_network, transaction_hash, created_at,
        customer_email, paystack_reference, paystack_ngn_amount,
        fx_rate_usd_ngn, fx_source, activated_at
    ) = order

    if payment_status == "paid":
        await safe_edit_message_text(query, 
            "✅ PAYMENT ALREADY VERIFIED\n\n"
            f"🆔 Order: {order_id}\n\n"
            "This order has already been marked as paid and active."
        )
        return

    if not paystack_reference:
        await safe_edit_message_text(query, 
            "❌ PAYSTACK REFERENCE MISSING\n\n"
            f"🆔 Order: {order_id}\n\n"
            "We couldn't find the Paystack reference for this checkout. "
            "Please start the Bank/Card payment again."
        )
        return

    # Show immediate feedback so the customer never sees an apparently dead button.
    await safe_edit_message_text(query, 
        "⏳ CHECKING PAYMENT\n\n"
        f"🆔 Order: {order_id}\n\n"
        "VibeScale is checking the transaction directly with Paystack..."
    )

    payment, error = await verify_paystack_transaction(paystack_reference)

    if error:
        print(f"[PAYSTACK VERIFY ERROR] order_id={order_id}: {error}")
        await safe_edit_message_text(query, 
            "⚠️ PAYMENT CHECK COULD NOT BE COMPLETED\n\n"
            f"🆔 Order: {order_id}\n\n"
            f"{error}\n\n"
            "Please wait a moment and tap 'I've Completed Payment' again."
        )
        return

    expected_ngn_amount = paystack_ngn_amount
    if expected_ngn_amount is None:
        await safe_edit_message_text(query, 
            "❌ PAYMENT AMOUNT NOT FOUND\n\n"
            f"🆔 Order: {order_id}\n\n"
            "The expected NGN amount is missing from this order. "
            "Please contact VibeScale Support."
        )
        return

    expected_amount = int(expected_ngn_amount) * 100
    paid_amount = payment.get("amount")
    paid_currency = payment.get("currency")
    paid_reference = payment.get("reference")
    payment_status_from_paystack = payment.get("status")

    print(
        "[PAYSTACK VERIFY RESULT] "
        f"order_id={order_id} status={payment_status_from_paystack} "
        f"amount={paid_amount} currency={paid_currency} "
        f"reference={paid_reference} expected={expected_amount}"
    )

    # IMPORTANT: An unpaid/abandoned checkout is a normal result, not an error.
    if payment_status_from_paystack != "success":
        await safe_edit_message_text(query, 
            "⏳ PAYMENT NOT COMPLETED YET\n\n"
            f"🆔 Order: {order_id}\n\n"
            "Paystack has not recorded a successful payment for this checkout yet.\n\n"
            "If you haven't paid yet, complete the Paystack checkout first, "
            "then return here and tap 'I've Completed Payment'.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🔄 Check Payment Again",
                    callback_data=f"paystack_verify_{order_id}"
                )],
                [InlineKeyboardButton(
                    "⬅️ Payment Methods",
                    callback_data="payment_start"
                )],
            ])
        )
        return

    if (
        paid_reference != paystack_reference
        or paid_amount != expected_amount
        or paid_currency != PAYSTACK_CURRENCY
    ):
        print(
            f"[PAYSTACK VERIFY MISMATCH] order_id={order_id} "
            f"expected_ref={paystack_reference} actual_ref={paid_reference} "
            f"expected_amount={expected_amount} actual_amount={paid_amount} "
            f"expected_currency={PAYSTACK_CURRENCY} actual_currency={paid_currency}"
        )
        await safe_edit_message_text(query, 
            "❌ PAYMENT COULD NOT BE VERIFIED\n\n"
            f"🆔 Order: {order_id}\n\n"
            "The transaction details do not match the amount/reference expected "
            "for this order. The order has NOT been activated.\n\n"
            "Please contact VibeScale Support if you believe you paid successfully."
        )
        return

    update_payment_status(order_id, "paid", "active")

    await safe_edit_message_text(query, 
        "✅ PAYMENT VERIFIED\n\n"
        f"🆔 Order ID: {order_id}\n"
        f"💵 VibeScale price: ${float(total_price):.2f} USD\n"
        f"🇳🇬 Paystack amount: ₦{int(expected_ngn_amount):,} NGN\n"
        f"🔗 Reference: {paystack_reference}\n\n"
        "💳 Payment: PAID\n"
        "📊 Order: ACTIVE\n\n"
        "Your VibeScale order is now active and ready for campaign processing."
    )

    await context.bot.send_message(
        chat_id=ADMIN_USER_ID,
        text=(
            "💰 VIBESCALE PAYSTACK PAYMENT VERIFIED\n\n"
            f"🆔 Order ID: {order_id}\n"
            f"👤 Customer: @{telegram_username or 'No username'}\n"
            f"🎯 X username: {x_username}\n"
            f"📦 Plan: {plan}\n"
            f"💵 VibeScale price: ${float(total_price):.2f} USD\n"
            f"🇳🇬 Paystack amount: ₦{int(expected_ngn_amount):,} NGN\n"
            f"📧 Email: {customer_email or 'N/A'}\n"
            f"🔗 Reference: {paystack_reference}\n\n"
            "💳 Payment: PAID\n"
            "📊 Order: ACTIVE"
        )
    )



def standard_campaign_targets(plan):
    return {
        "Starter": {"posts": 30, "likes": 1500, "reposts": 300, "bookmarks": 300, "views": 30000, "comments": 300},
        "Growth": {"posts": 30, "likes": 3000, "reposts": 600, "bookmarks": 600, "views": 60000, "comments": 600},
        "Scale": {"posts": 30, "likes": 6000, "reposts": 1200, "bookmarks": 1200, "views": 120000, "comments": 1200},
    }.get(plan, {"posts": 0, "likes": 0, "reposts": 0, "bookmarks": 0, "views": 0, "comments": 0})

def ensure_campaign_for_order(order):
    order_id, _, _, plan, _, _, _, _, _, _, _, _, _, _, _, _, _, _ = order
    targets = standard_campaign_targets(plan)
    if plan.startswith("Custom (") and plan.endswith(")"):
        request_id = plan[len("Custom ("):-1]
        custom = get_custom_quote(request_id)
        if custom:
            targets = {"posts": custom[4], "likes": custom[5], "reposts": custom[6], "bookmarks": custom[7], "views": custom[8], "comments": custom[9]}
    create_or_update_campaign(order_id, targets)
    return get_campaign_metrics(order_id)

def progress_pct(delivered, target):
    if target <= 0:
        return 0
    return min(100, round((delivered / target) * 100))

def metric_lines(metrics):
    if not metrics:
        return ""
    (_, tp, tl, tr, tb, tv, tc, dp, dl, dr, db, dv, dc, _, _) = metrics
    return (
        "📈 CAMPAIGN PROGRESS\n"
        f"📝 Posts: {dp:,}/{tp:,} ({progress_pct(dp,tp)}%)\n"
        f"❤️ Likes: {dl:,}/{tl:,} ({progress_pct(dl,tl)}%)\n"
        f"🔁 Reposts: {dr:,}/{tr:,} ({progress_pct(dr,tr)}%)\n"
        f"🔖 Bookmarks: {db:,}/{tb:,} ({progress_pct(db,tb)}%)\n"
        f"👀 Views: {dv:,}/{tv:,} ({progress_pct(dv,tv)}%)\n"
        f"💬 Comments: {dc:,}/{tc:,} ({progress_pct(dc,tc)}%)"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer_callback(query)

    # ADMIN PAYMENT VERIFICATION
    if query.data.startswith("admin_verify_") or query.data.startswith("admin_reject_"):
        if update.effective_user.id != ADMIN_USER_ID:
            await safe_answer_callback(query, "❌ Admin access only.", show_alert=True)
            return

        approved = query.data.startswith("admin_verify_")
        order_id = (
            query.data.replace("admin_verify_", "", 1)
            if approved
            else query.data.replace("admin_reject_", "", 1)
        )

        order = get_order_by_id(order_id)
        if not order:
            await safe_edit_message_text(query, "❌ Order not found.")
            return

        (
            db_order_id, telegram_user_id, telegram_username, plan,
            x_username, duration_days, total_price, payment_status,
            order_status, payment_network, transaction_hash, created_at,
            customer_email, paystack_reference, paystack_ngn_amount,
            fx_rate_usd_ngn, fx_source, activated_at
        ) = order

        if payment_status != "pending":
            await safe_answer_callback(
                query,
                f"Payment already marked {payment_status.upper()}.",
                show_alert=True,
            )
            return

        if approved:
            update_payment_status(order_id, "paid", "active")
            await safe_edit_message_text(query, 
                "✅ PAYMENT VERIFIED\n\n"
                f"🆔 Order ID: {order_id}\n"
                f"💵 Amount: ${total_price:.2f}\n"
                f"🌐 Network: {payment_network}\n"
                f"🔗 TXID: {transaction_hash}\n\n"
                "💳 Payment: PAID\n"
                "📊 Order: ACTIVE"
            )
        else:
            update_payment_status(order_id, "rejected", "pending")
            await safe_edit_message_text(query, 
                "❌ PAYMENT REJECTED\n\n"
                f"🆔 Order ID: {order_id}\n"
                f"💵 Amount: ${total_price:.2f}\n"
                f"🌐 Network: {payment_network}\n"
                f"🔗 TXID: {transaction_hash}\n\n"
                "💳 Payment: REJECTED\n"
                "📊 Order: PENDING"
            )

        await notify_customer_payment_result(
            context, telegram_user_id, order_id, approved
        )
        return

    # MAIN MENU
    if query.data == "home":
        keyboard = [
            [InlineKeyboardButton("📦 Plans", callback_data="plans"), InlineKeyboardButton("🛒 Create Order", callback_data="order")],
            [InlineKeyboardButton("🧩 Custom Plan", callback_data="custom"), InlineKeyboardButton("📊 My Campaign", callback_data="campaign")],
            [InlineKeyboardButton("📋 My Orders", callback_data="orders"), InlineKeyboardButton("💳 Payments & Wallet", callback_data="payment")],
            [InlineKeyboardButton("🆘 Support", callback_data="support"), InlineKeyboardButton("ℹ️ How It Works", callback_data="help")],
        ]
        await safe_edit_message_text(query, "🚀 Welcome to VibeScale!\n\nX/Twitter content amplification made simple.\n\nChoose an option below:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # PLANS
    if query.data == "plans":
        text = (
            "📦 VibeScale Plans\n\n"
            "🥉 Starter — $106.50/week\n"
            "Up to 30 posts\n"
            "1,500 likes • 300 reposts\n"
            "300 bookmarks • 30K views\n"
            "300 comments\n\n"

            "🥈 Growth — $213/week\n"
            "Up to 30 posts\n"
            "3,000 likes • 600 reposts\n"
            "600 bookmarks • 60K views\n"
            "600 comments\n\n"

            "🥇 Scale — $426/week\n"
            "Up to 30 posts\n"
            "6,000 likes • 1,200 reposts\n"
            "1,200 bookmarks • 120K views\n"
            "1,200 comments"
        )

        await safe_edit_message_text(query, text)
        return

    # CREATE ORDER
    if query.data == "order":
        keyboard = [
            [
                InlineKeyboardButton(
                    "🥉 Starter — $106.50/week",
                    callback_data="plan_starter"
                )
            ],
            [
                InlineKeyboardButton(
                    "🥈 Growth — $213/week",
                    callback_data="plan_growth"
                )
            ],
            [
                InlineKeyboardButton(
                    "🥇 Scale — $426/week",
                    callback_data="plan_scale"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="cancel_order"
                )
            ],
        ]

        await safe_edit_message_text(query, 
            "🛒 Create Your Order\n\n"
            "First, choose your VibeScale plan:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # PLAN SELECTION
    if query.data in ["plan_starter", "plan_growth", "plan_scale"]:

        plans = {
            "plan_starter": ("Starter", 106.50),
            "plan_growth": ("Growth", 213.00),
            "plan_scale": ("Scale", 426.00),
        }

        plan_name, weekly_price = plans[query.data]

        context.user_data["selected_plan"] = plan_name
        context.user_data["weekly_price"] = weekly_price

        await safe_edit_message_text(query, 
            f"✅ Plan selected: {plan_name}\n"
            f"💵 Weekly price: ${weekly_price:.2f}\n\n"
            "Now send me your X/Twitter username.\n\n"
            "Example:\n"
            "@VibeScale"
        )

        context.user_data["waiting_for_username"] = True
        return

    # DURATION
    if query.data in ["duration_7", "duration_14", "duration_30"]:

        duration_map = {
            "duration_7": 7,
            "duration_14": 14,
            "duration_30": 30,
        }

        days = duration_map[query.data]

        weekly_price = context.user_data.get("weekly_price", 0)
        plan = context.user_data.get("selected_plan", "Unknown")
        username = context.user_data.get("x_username", "Unknown")

        total_price = weekly_price * (days / 7)

        context.user_data["duration"] = days
        context.user_data["total_price"] = total_price

        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Confirm Order",
                    callback_data="confirm_order"
                )
            ],
            [
                InlineKeyboardButton(
                    "✏️ Change Details",
                    callback_data="order"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="cancel_order"
                )
            ],
        ]

        await safe_edit_message_text(query, 
            "🧾 ORDER REVIEW\n\n"
            f"📦 Plan: {plan}\n"
            f"👤 X username: {username}\n"
            f"📅 Duration: {days} days\n"
            f"💵 Total: ${total_price:.2f}\n\n"
            "Please review your order before continuing.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # CONFIRM ORDER + SAVE TO DATABASE
    if query.data == "confirm_order":

        plan = context.user_data.get("selected_plan")
        username = context.user_data.get("x_username")
        days = context.user_data.get("duration")
        total_price = context.user_data.get("total_price")

        # Create a unique VibeScale order ID
        order_id = f"VS-{random.randint(100000, 999999)}"

        # Telegram customer information
        telegram_user_id = update.effective_user.id
        telegram_username = update.effective_user.username

        # Save order permanently
        create_order(
            order_id=order_id,
            telegram_user_id=telegram_user_id,
            telegram_username=telegram_username,
            plan=plan,
            x_username=username,
            duration_days=days,
            total_price=total_price,
        )

        context.user_data["order_id"] = order_id
        context.user_data["order_confirmed"] = True

        keyboard = [
            [
                InlineKeyboardButton(
                    "💳 Proceed to Payment",
                    callback_data="payment_start"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel Order",
                    callback_data="cancel_order"
                )
            ],
        ]

        await safe_edit_message_text(query, 
            "✅ ORDER CONFIRMED\n\n"
            f"🆔 Order ID: {order_id}\n"
            f"📦 Plan: {plan}\n"
            f"👤 X username: {username}\n"
            f"📅 Duration: {days} days\n"
            f"💵 Total: ${total_price:.2f}\n"
            f"💳 Payment: Pending\n\n"
            "Your order has been saved successfully.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

        # PAYMENT MENU
    if query.data == "payment_start":

        # Prefer the current session, but fall back to SQLite so this button
        # still works if the Telegram session lost its temporary data.
        order_id = context.user_data.get("order_id")
        total_price = context.user_data.get("total_price")

        if order_id and total_price is None:
            order = get_order_by_id(order_id)
            if order:
                total_price = order[6]
                context.user_data["total_price"] = total_price
                context.user_data["selected_plan"] = order[3]
                context.user_data["x_username"] = order[4]
                context.user_data["duration"] = order[5]

        if not order_id or total_price is None:
            await safe_edit_message_text(query, 
                "❌ No active order was found.\n\n"
                "Please use /start to create a new order."
            )
            return

        keyboard = [
            [
                InlineKeyboardButton(
                    "💳 Bank / Card",
                    callback_data="pay_bank_card"
                )
            ],
            [
                InlineKeyboardButton(
                    "₿ Crypto",
                    callback_data="pay_crypto"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="cancel_order"
                )
            ],
        ]

        await safe_edit_message_text(query, 
            "💳 VIBESCALE PAYMENT\n\n"
            f"🆔 Order: {order_id}\n"
            f"💵 Amount: ${total_price:.2f}\n\n"
            "Choose your payment method:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # CRYPTO PAYMENT
    if query.data == "pay_crypto":

        keyboard = [
            [
                InlineKeyboardButton(
                    "💎 USDT — TON",
                    callback_data="crypto_USDT_TON"
                )
            ],
            [
                InlineKeyboardButton(
                    "🟣 USDT — Solana",
                    callback_data="crypto_USDT_SOLANA"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔴 USDT — TRC20",
                    callback_data="crypto_USDT_TRC20"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔵 USDT — ERC20",
                    callback_data="crypto_USDT_ERC20"
                )
            ],
            [
                InlineKeyboardButton(
                    "🟡 USDT — BEP20",
                    callback_data="crypto_USDT_BEP20"
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data="payment_start"
                )
            ],
        ]

        await safe_edit_message_text(query, 
            "₿ CRYPTO PAYMENT\n\n"
            "Choose the USDT network you want to use:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # CRYPTO ADDRESS
    if query.data.startswith("crypto_") and not query.data.startswith("crypto_paid_"):

        crypto_key = query.data.replace("crypto_", "")
        crypto = CRYPTO_ADDRESSES.get(crypto_key)

        if not crypto:
            await safe_edit_message_text(query, 
                "❌ Payment network not found."
            )
            return

        order_id = context.user_data.get("order_id")
        total_price = context.user_data.get("total_price")

        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ I've Paid",
                    callback_data=f"crypto_paid_{crypto_key}"
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Change Network",
                    callback_data="pay_crypto"
                )
            ],
        ]

        await safe_edit_message_text(query, 
            "₿ VIBESCALE CRYPTO PAYMENT\n\n"
            f"🆔 Order: {order_id}\n"
            f"💵 Amount: ${total_price:.2f}\n"
            f"🌐 Network: {crypto['network']}\n\n"
            "📍 SEND USDT TO:\n"
            f"{crypto['address']}\n\n"
            "⚠️ IMPORTANT:\n"
            f"Send USDT using {crypto['network']} ONLY.\n"
            "Sending through another network may result in loss of funds.\n\n"
            "After sending the payment, tap \"I've Paid\".",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

        # CUSTOMER SAYS PAYMENT WAS SENT
    if query.data.startswith("crypto_paid_"):

        crypto_key = query.data.replace("crypto_paid_", "")
        crypto = CRYPTO_ADDRESSES.get(crypto_key)

        if not crypto:
            await safe_edit_message_text(query, "❌ Payment network not found.")
            return

        order_id = context.user_data.get("order_id")
        total_price = context.user_data.get("total_price")

        if not order_id or not total_price:
            await safe_edit_message_text(query, 
                "❌ No active order was found.\n\n"
                "Please use /start to create an order."
            )
            return

        # Remember the selected network while we wait for the transaction hash.
        context.user_data["payment_crypto_key"] = crypto_key
        context.user_data["waiting_for_tx_hash"] = True

        await safe_edit_message_text(query, 
            "🔐 PAYMENT VERIFICATION\n\n"
            f"🆔 Order: {order_id}\n"
            f"💵 Amount: ${total_price:.2f}\n"
            f"🌐 Network: {crypto['network']}\n\n"
            "Please send your transaction hash / TXID below.\n\n"
            "This lets VibeScale verify the payment on the blockchain.\n\n"
            "⚠️ Do not send your wallet seed phrase or private key.\n"
            "Only send the transaction hash / TXID."
        )
        return

    # BANK / CARD PAYMENT
    if query.data == "pay_bank_card":

        order_id = context.user_data.get("order_id")
        total_price = context.user_data.get("total_price")

        if not order_id or total_price is None:
            await safe_edit_message_text(query, 
                "❌ No active order was found.\n\n"
                "Please use /start to create an order."
            )
            return

        # Paystack requires a customer email when initializing a transaction.
        context.user_data["waiting_for_paystack_email"] = True

        await safe_edit_message_text(query, 
            "💳 BANK / CARD PAYMENT\n\n"
            f"🆔 Order: {order_id}\n"
            f"💵 Amount: ${total_price:.2f}\n\n"
            "Please send the email address you want to use for this payment.\n\n"
            "Example: customer@example.com"
        )
        return

    # PAYSTACK PAYMENT VERIFICATION
    if query.data.startswith("paystack_verify_"):

        order_id = query.data.replace("paystack_verify_", "", 1)
        order = get_order_by_id(order_id)

        if not order:
            await safe_answer_callback(query, "❌ Order not found.", show_alert=True)
            return

        (
            db_order_id, telegram_user_id, telegram_username, plan,
            x_username, duration_days, total_price, payment_status,
            order_status, payment_network, transaction_hash, created_at,
            customer_email, paystack_reference, paystack_ngn_amount,
            fx_rate_usd_ngn, fx_source, activated_at
        ) = order

        if payment_status == "paid":
            await safe_answer_callback(query, "Payment is already marked as paid.", show_alert=True)
            return

        if not paystack_reference:
            await safe_answer_callback(query, "❌ Paystack reference not found.", show_alert=True)
            return

        payment, error = await verify_paystack_transaction(paystack_reference)

        if error:
            await safe_answer_callback(query, f"❌ {error}", show_alert=True)
            return

        expected_ngn_amount = paystack_ngn_amount
        if expected_ngn_amount is None:
            await safe_answer_callback(query, "❌ No NGN payment amount is stored for this order.", show_alert=True)
            return
        expected_amount = int(expected_ngn_amount) * 100
        paid_amount = payment.get("amount")
        paid_currency = payment.get("currency")
        paid_reference = payment.get("reference")
        payment_status_from_paystack = payment.get("status")

        if (
            payment_status_from_paystack != "success"
            or paid_reference != paystack_reference
            or paid_amount != expected_amount
            or paid_currency != PAYSTACK_CURRENCY
        ):
            await safe_answer_callback(
                query,
                "❌ Payment has not been verified for the expected amount/reference.",
                show_alert=True,
            )
            return

        update_payment_status(order_id, "paid", "active")

        await safe_edit_message_text(query, 
            "✅ PAYMENT VERIFIED\n\n"
            f"🆔 Order ID: {order_id}\n"
            f"💵 VibeScale price: ${total_price:.2f} USD\n"
             f"🇳🇬 Paystack amount: ₦{int(expected_ngn_amount):,} NGN\n"
            f"🔗 Reference: {paystack_reference}\n\n"
            "💳 Payment: PAID\n"
            "📊 Order: ACTIVE\n\n"
            "Your VibeScale order is now active and ready for campaign processing."
        )

        await context.bot.send_message(
            chat_id=telegram_user_id,
            text=(
                "✅ PAYMENT VERIFIED\n\n"
                f"🆔 Order: {order_id}\n\n"
                "Your Paystack payment has been verified successfully.\n"
                "Your VibeScale order is now active and ready for campaign processing."
            ),
        )

        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=(
                "💰 VIBESCALE PAYSTACK PAYMENT VERIFIED\n\n"
                f"🆔 Order ID: {order_id}\n"
                f"👤 Customer: @{telegram_username or 'No username'}\n"
                f"🎯 X username: {x_username}\n"
                f"📦 Plan: {plan}\n"
                f"💵 VibeScale price: ${total_price:.2f} USD\n"
             f"🇳🇬 Paystack amount: ₦{int(expected_ngn_amount):,} NGN\n"
                f"📧 Email: {customer_email or 'N/A'}\n"
                f"🔗 Reference: {paystack_reference}\n\n"
                "💳 Payment: PAID\n"
                "📊 Order: ACTIVE"
            ),
        )
        return

    # MY ORDERS
    if query.data == "orders":

        telegram_user_id = update.effective_user.id
        orders = get_user_orders(telegram_user_id)

        if not orders:
            await safe_edit_message_text(query, 
                "📋 My Orders\n\n"
                "You don't have any orders yet."
            )
            return

        text = "📋 YOUR VIBESCALE ORDERS\n\n"

        for order in orders:
            (
                order_id,
                plan,
                x_username,
                duration_days,
                total_price,
                payment_status,
                order_status,
                created_at,
                activated_at,
            ) = order

            text += (
                f"🆔 {order_id}\n"
                f"📦 {plan}\n"
                f"👤 {x_username}\n"
                f"📅 {duration_days} days\n"
                f"💵 ${total_price:.2f}\n"
                f"💳 Payment: {payment_status}\n"
                f"📊 Status: {order_status}\n\n"
            )

        await safe_edit_message_text(query, text, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 My Campaign", callback_data="campaign")],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data="home")],
        ]))
        return

    # CANCEL
    if query.data == "cancel_order":

        context.user_data.clear()

        await safe_edit_message_text(query, 
            "❌ Order cancelled.\n\n"
            "Use /start whenever you're ready to begin again."
        )
        return

    # CUSTOM PLAN
    if query.data == "custom":
        context.user_data.clear()
        context.user_data["custom_plan"] = {}
        context.user_data["waiting_custom_x_username"] = True
        await safe_edit_message_text(query,
            "🧩 CUSTOM PLAN BUILDER\n\n"
            "Let's build a campaign around exactly what you need.\n\n"
            "First, send the X/Twitter username you want to run the campaign for.\n\n"
            "Example: @VibeScale"
        )
        return

    # CUSTOM PLAN DURATION
    if query.data in ["custom_duration_7", "custom_duration_14", "custom_duration_30"]:
        duration_map = {
            "custom_duration_7": 7,
            "custom_duration_14": 14,
            "custom_duration_30": 30,
        }
        context.user_data.setdefault("custom_plan", {})["duration_days"] = duration_map[query.data]
        context.user_data["waiting_custom_notes"] = True
        await safe_edit_message_text(query,
            "📝 SPECIAL REQUIREMENTS\n\n"
            "Any special requirements, targeting notes, or instructions?\n\n"
            "Send them now, or tap 'No special requirements'.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("No special requirements", callback_data="custom_no_notes")
            ], [
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_custom")
            ]])
        )
        return

    if query.data == "custom_no_notes":
        context.user_data.setdefault("custom_plan", {})["notes"] = "None"
        context.user_data["waiting_custom_notes"] = False
        await show_custom_review(update, context)
        return

    if query.data == "custom_submit":
        data = context.user_data.get("custom_plan", {})
        required = ["x_username", "posts", "likes", "reposts", "bookmarks", "views", "comments", "duration_days", "notes"]
        if any(key not in data for key in required):
            await safe_edit_message_text(query, "❌ Your custom plan request is incomplete. Please start again from the main menu.")
            return

        customer = update.effective_user
        request_id = f"VSC-{random.randint(100000, 999999)}"
        create_custom_quote(
            request_id=request_id,
            telegram_user_id=customer.id,
            telegram_username=customer.username,
            x_username=data["x_username"],
            posts=data["posts"],
            likes=data["likes"],
            reposts=data["reposts"],
            bookmarks=data["bookmarks"],
            views=data["views"],
            comments=data["comments"],
            duration_days=data["duration_days"],
            notes=data["notes"],
        )

        await safe_edit_message_text(query,
            "✅ CUSTOM PLAN REQUEST SUBMITTED\n\n"
            f"🆔 Request: {request_id}\n"
            f"🎯 X: {data['x_username']}\n"
            f"📅 Duration: {data['duration_days']} days\n\n"
            "Your requirements have been sent to VibeScale.\n"
            "We will review them and send you the custom price before any payment is requested.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 My Custom Requests", callback_data="custom_requests")
            ], [
                InlineKeyboardButton("⬅️ Main Menu", callback_data="home")
            ]])
        )

        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=(
                "🧩 NEW VIBESCALE CUSTOM PLAN REQUEST\n\n"
                f"🆔 Request: {request_id}\n"
                f"👤 Customer: @{customer.username or 'No username'}\n"
                f"🎯 X username: {data['x_username']}\n\n"
                f"📅 Duration: {data['duration_days']} days\n"
                f"📝 Posts: {data['posts']:,}\n"
                f"❤️ Likes: {data['likes']:,}\n"
                f"🔁 Reposts: {data['reposts']:,}\n"
                f"🔖 Bookmarks: {data['bookmarks']:,}\n"
                f"👀 Views: {data['views']:,}\n"
                f"💬 Comments: {data['comments']:,}\n"
                f"📌 Notes: {data['notes']}\n\n"
                "💵 Status: AWAITING CUSTOM QUOTE"
            )
        )
        context.user_data.clear()
        return

    if query.data == "custom_requests":
        telegram_user_id = update.effective_user.id
        requests = get_user_custom_quotes(telegram_user_id)
        if not requests:
            text = "🧩 MY CUSTOM REQUESTS\n\nYou don't have any custom plan requests yet."
            keyboard = [
                [InlineKeyboardButton("🧩 New Custom Plan", callback_data="custom")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="home")],
            ]
        else:
            text = "🧩 MY CUSTOM REQUESTS\n\n"
            keyboard = []
            for req in requests:
                request_id, x_username, duration_days, status, created_at = req
                text += (
                    f"🆔 {request_id}\n"
                    f"🎯 X: {x_username}\n"
                    f"📅 {duration_days} days\n"
                    f"📊 {status.replace('_', ' ').upper()}\n"
                    f"🕒 {created_at[:10]}\n\n"
                )
                keyboard.append([
                    InlineKeyboardButton(
                        f"🔎 View {request_id}",
                        callback_data=f"custom_view_{request_id}"
                    )
                ])
            keyboard += [
                [InlineKeyboardButton("🧩 New Custom Plan", callback_data="custom")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="home")],
            ]

        await safe_edit_message_text(
            query, text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if query.data.startswith("custom_view_"):
        request_id = query.data.replace("custom_view_", "", 1)
        request = get_custom_quote(request_id)
        if not request or request[1] != update.effective_user.id:
            await safe_answer_callback(query, "❌ Request not found.", show_alert=True)
            return

        (
            req_id, user_id, tg_user, x_username, posts, likes, reposts,
            bookmarks, views, comments, duration, notes, status, created_at,
            quoted_price, admin_note, updated_at
        ) = request

        text = (
            "🧩 CUSTOM PLAN REQUEST\n\n"
            f"🆔 {req_id}\n"
            f"🎯 X: {x_username}\n"
            f"📅 Duration: {duration} days\n\n"
            f"📝 Posts: {posts:,}\n"
            f"❤️ Likes: {likes:,}\n"
            f"🔁 Reposts: {reposts:,}\n"
            f"🔖 Bookmarks: {bookmarks:,}\n"
            f"👀 Views: {views:,}\n"
            f"💬 Comments: {comments:,}\n\n"
            f"📊 Status: {status.replace('_', ' ').upper()}\n"
        )
        if quoted_price is not None:
            text += f"💵 Custom quote: ${float(quoted_price):,.2f}\n"
        if admin_note:
            text += f"📌 Admin note: {admin_note}\n"

        buttons = []
        if status in ("awaiting_quote", "quoted"):
            buttons.append([
                InlineKeyboardButton(
                    "❌ Cancel Request",
                    callback_data=f"cancel_custom_request_{req_id}"
                )
            ])
        if status == "quoted":
            buttons.insert(0, [
                InlineKeyboardButton(
                    "✅ Accept Quote",
                    callback_data=f"custom_accept_{req_id}"
                ),
                InlineKeyboardButton(
                    "❌ Decline Quote",
                    callback_data=f"custom_decline_{req_id}"
                ),
            ])
        buttons += [
            [InlineKeyboardButton("⬅️ My Custom Requests", callback_data="custom_requests")],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data="home")],
        ]
        await safe_edit_message_text(query, text, reply_markup=InlineKeyboardMarkup(buttons))
        return

    if query.data.startswith("cancel_custom_request_"):
        request_id = query.data.replace("cancel_custom_request_", "", 1)
        changed = cancel_custom_quote(request_id, update.effective_user.id)
        if not changed:
            await safe_answer_callback(
                query,
                "This request can no longer be cancelled.",
                show_alert=True
            )
            return

        await safe_edit_message_text(
            query,
            "❌ CUSTOM REQUEST CANCELLED\n\n"
            f"🆔 Request: {request_id}\n\n"
            "No payment is due for this request.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧩 My Custom Requests", callback_data="custom_requests")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="home")],
            ])
        )
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"❌ CUSTOM PLAN REQUEST CANCELLED BY CUSTOMER\n\n🆔 Request: {request_id}"
        )
        return

    if query.data.startswith("custom_accept_"):
        request_id = query.data.replace("custom_accept_", "", 1)
        request = get_custom_quote(request_id)
        if not request or request[1] != update.effective_user.id:
            await safe_answer_callback(query, "❌ Request not found.", show_alert=True)
            return
        if request[12] != "quoted" or request[14] is None:
            await safe_answer_callback(query, "❌ This quote is no longer available.", show_alert=True)
            return

        (
            req_id, user_id, tg_user, x_username, posts, likes, reposts,
            bookmarks, views, comments, duration, notes, status, created_at,
            quoted_price, admin_note, updated_at
        ) = request

        # Turn the accepted custom quote into a normal payable VibeScale order.
        order_id = f"VS-{random.randint(100000, 999999)}"
        create_order(
            order_id=order_id,
            telegram_user_id=user_id,
            telegram_username=tg_user,
            plan=f"Custom ({request_id})",
            x_username=x_username,
            duration_days=duration,
            total_price=float(quoted_price),
        )
        update_custom_quote(request_id, status="accepted")
        context.user_data.update({
            "order_id": order_id,
            "order_confirmed": True,
            "selected_plan": f"Custom ({request_id})",
            "x_username": x_username,
            "duration": duration,
            "total_price": float(quoted_price),
        })

        await safe_edit_message_text(
            query,
            "✅ CUSTOM QUOTE ACCEPTED\n\n"
            f"🆔 Request: {request_id}\n"
            f"🛒 Order: {order_id}\n"
            f"💵 Quote: ${float(quoted_price):,.2f}\n\n"
            "Your custom quote has been accepted. Choose a payment method to continue.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Proceed to Payment", callback_data="payment_start")],
                [InlineKeyboardButton("📋 My Orders", callback_data="orders")],
            ])
        )
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=(
                "✅ CUSTOMER ACCEPTED CUSTOM QUOTE\n\n"
                f"🆔 Request: {request_id}\n"
                f"🛒 Order: {order_id}\n"
                f"💵 Quote: ${float(quoted_price):,.2f}\n"
                f"👤 Customer: @{tg_user or 'No username'}"
            )
        )
        return

    if query.data.startswith("custom_decline_"):
        request_id = query.data.replace("custom_decline_", "", 1)
        request = get_custom_quote(request_id)
        if not request or request[1] != update.effective_user.id:
            await safe_answer_callback(query, "❌ Request not found.", show_alert=True)
            return
        if request[12] != "quoted":
            await safe_answer_callback(query, "This quote is no longer active.", show_alert=True)
            return

        update_custom_quote(request_id, status="declined")
        await safe_edit_message_text(
            query,
            "❌ CUSTOM QUOTE DECLINED\n\n"
            f"🆔 Request: {request_id}\n\n"
            "No payment is due. You can submit a new custom request whenever you're ready.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧩 New Custom Plan", callback_data="custom")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="home")],
            ])
        )
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"❌ CUSTOMER DECLINED CUSTOM QUOTE\n\n🆔 Request: {request_id}"
        )
        return

    if query.data == "cancel_custom":
        context.user_data.clear()
        await safe_edit_message_text(query, "❌ Custom plan request cancelled.", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Main Menu", callback_data="home")
        ]]))
        return

    # CAMPAIGN DASHBOARD
    if query.data == "campaign":
        telegram_user_id = update.effective_user.id
        orders = get_user_orders(telegram_user_id)
        from datetime import timedelta
        active = []
        now = datetime.now()
        for order_summary in orders:
            order_id, plan, x_username, duration_days, total_price, payment_status, order_status, created_at, activated_at = order_summary
            if payment_status != "paid" or order_status not in ("active", "paused"):
                continue
            full_order = get_order_by_id(order_id)
            metrics = ensure_campaign_for_order(full_order)
            try:
                start_value = datetime.fromisoformat(activated_at or created_at)
                end_value = start_value + timedelta(days=int(duration_days))
            except (TypeError, ValueError):
                continue
            remaining = max(0, (end_value.date() - now.date()).days)
            active.append((order_id, plan, x_username, duration_days, total_price, start_value, end_value, remaining, order_status, metrics))

        if not active:
            await safe_edit_message_text(query,
                "📊 MY CAMPAIGN\
\
"
                "You don't have an active campaign yet.\
\
"
                "Once your payment is verified, your campaign will appear here.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 My Orders", callback_data="orders")],[InlineKeyboardButton("⬅️ Main Menu", callback_data="home")]])
            )
            return

        text = "📊 MY ACTIVE CAMPAIGN" + ("S" if len(active) > 1 else "") + "\
\
"
        keyboard=[]
        for item in active:
            order_id, plan, x_username, duration_days, total_price, start_value, end_value, remaining, order_status, metrics = item
            status_label = "⏸ PAUSED" if order_status == "paused" else "🟢 ACTIVE"
            text += (f"🆔 Order: {order_id}\
📦 Plan: {plan}\
👤 X: {x_username}\
"
                     f"📅 Duration: {duration_days} days\
{status_label}\
"
                     f"🚀 Started: {start_value.strftime('%d %b %Y')}\
🏁 Ends: {end_value.strftime('%d %b %Y')}\
"
                     f"⏳ Days remaining: {remaining}\
💵 Plan value: ${float(total_price):.2f}\
\
"
                     + metric_lines(metrics) + "\
\
")
            keyboard.append([InlineKeyboardButton(f"🔎 Details {order_id}", callback_data=f"campaign_view_{order_id}")])
        keyboard += [[InlineKeyboardButton("🔄 Refresh", callback_data="campaign")],[InlineKeyboardButton("📋 My Orders", callback_data="orders")],[InlineKeyboardButton("⬅️ Main Menu", callback_data="home")]]
        await safe_edit_message_text(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if query.data.startswith("campaign_view_"):
        order_id = query.data.replace("campaign_view_", "", 1)
        order = get_order_by_id(order_id)
        if not order or order[1] != update.effective_user.id or order[7] != "paid":
            await safe_answer_callback(query, "❌ Campaign not found.", show_alert=True)
            return
        metrics = ensure_campaign_for_order(order)
        from datetime import timedelta
        start_value = datetime.fromisoformat(order[17] or order[11])
        end_value = start_value + timedelta(days=int(order[5]))
        remaining = max(0, (end_value.date() - datetime.now().date()).days)
        text = (f"📊 CAMPAIGN DETAILS\
\
🆔 Order: {order_id}\
📦 {order[3]}\
🎯 X: {order[4]}\
"
                f"📊 Status: {order[8].upper()}\
🚀 Started: {start_value.strftime('%d %b %Y')}\
🏁 Ends: {end_value.strftime('%d %b %Y')}\
"
                f"⏳ Days remaining: {remaining}\
💵 Value: ${float(order[6]):.2f}\
\
{metric_lines(metrics)}")
        await safe_edit_message_text(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data=f"campaign_view_{order_id}")],[InlineKeyboardButton("⬅️ My Campaign", callback_data="campaign")],[InlineKeyboardButton("⬅️ Main Menu", callback_data="home")]]))
        return

    # PAYMENT MENU
    if query.data == "payment":
        await safe_edit_message_text(query, 
            "💳 Payments & Wallet\n\n"
            "Payment options will be connected to your orders."
        )
        return

    # SUPPORT
    if query.data == "support":
        context.user_data["support_state"] = "subject"
        await safe_edit_message_text(
            query,
            "🆘 VIBESCALE SUPPORT\n\n"
            "Tell us what you need help with.\n\n"
            "First, send a short subject for your issue.\n\n"
            "Example: Paystack payment issue"
        )
        return

    if query.data == "support_tickets":
        tickets = get_user_support_tickets(update.effective_user.id)
        if not tickets:
            text = "🎫 MY SUPPORT TICKETS\n\nYou don't have any support tickets yet."
        else:
            text = "🎫 MY SUPPORT TICKETS\n\n"
            for ticket_id, subject, message, status, created_at, updated_at in tickets:
                text += (
                    f"🆔 {ticket_id}\n"
                    f"📌 {subject}\n"
                    f"📊 {status.upper()}\n"
                    f"🕒 {created_at[:10]}\n\n"
                )

        await safe_edit_message_text(
            query,
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ New Ticket", callback_data="support")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="home")],
            ])
        )
        return

    if query.data == "support_cancel":
        context.user_data.pop("support_state", None)
        await safe_edit_message_text(
            query,
            "❌ Support ticket creation cancelled.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🆘 Support", callback_data="support")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="home")],
            ])
        )
        return

    # ADMIN DASHBOARD
    if query.data == "admin_dashboard":
        if update.effective_user.id != ADMIN_USER_ID:
            await query.answer("❌ Admin access only.", show_alert=True)
            return

        stats = get_admin_order_stats()
        tickets = get_admin_support_tickets(10)
        custom_quotes = get_admin_custom_quotes(10)

        text = (
            "🛠️ VIBESCALE ADMIN DASHBOARD\n\n"
            f"📦 Total orders: {stats['total_orders']}\n"
            f"💳 Paid orders: {stats['paid_orders']}\n"
            f"📈 Active campaigns: {stats['active_orders']}\n"
            f"💰 Paid order value: ${float(stats['paid_value']):,.2f}\n\n"
            f"🎫 Open support tickets: {sum(1 for t in tickets if t[5] == 'open')}\n"
            f"🧩 Custom requests shown: {len(custom_quotes)}\n"
        )

        await safe_edit_message_text(
            query,
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 Recent Orders", callback_data="admin_orders")],
                [InlineKeyboardButton("📊 Campaigns", callback_data="admin_campaigns")],
                [InlineKeyboardButton("🎫 Support Tickets", callback_data="admin_support")],
                [InlineKeyboardButton("🧩 Custom Requests", callback_data="admin_custom")],
                [InlineKeyboardButton("🔄 Refresh", callback_data="admin_dashboard")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="home")],
            ])
        )
        return

    if query.data == "admin_campaigns":
        if update.effective_user.id != ADMIN_USER_ID:
            await safe_answer_callback(query, "❌ Admin access only.", show_alert=True)
            return
        rows = get_admin_campaigns(20)
        text = "📊 CAMPAIGN MANAGEMENT\
\
"
        keyboard=[]
        if not rows:
            text += "No paid campaigns yet."
        else:
            for row in rows:
                oid, uid, tg, plan, xu, dur, total, pay, status, created, activated, tp,tl,tr,tb,tv,tc,dp,dl,dr,db,dv,dc = row
                text += f"🆔 {oid} | {status.upper()}\
👤 @{tg or 'No username'}\
🎯 {xu} | 📦 {plan}\
"
                text += f"📈 Likes {dl:,}/{tl:,} • Views {dv:,}/{tv:,}\
\
"
                keyboard.append([InlineKeyboardButton(f"🔎 Manage {oid}", callback_data=f"admin_campaign_view_{oid}")])
        keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="admin_campaigns")])
        keyboard.append([InlineKeyboardButton("⬅️ Admin Dashboard", callback_data="admin_dashboard")])
        await safe_edit_message_text(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if query.data.startswith("admin_campaign_view_"):
        if update.effective_user.id != ADMIN_USER_ID:
            await safe_answer_callback(query, "❌ Admin access only.", show_alert=True)
            return
        order_id = query.data.replace("admin_campaign_view_", "", 1)
        order = get_order_by_id(order_id)
        if not order:
            await safe_answer_callback(query, "❌ Order not found.", show_alert=True)
            return
        metrics = ensure_campaign_for_order(order)
        text = f"🛠️ MANAGE CAMPAIGN\
\
🆔 {order_id}\
👤 @{order[2] or 'No username'}\
🎯 X: {order[4]}\
📦 {order[3]}\
📊 Status: {order[8].upper()}\
💵 ${float(order[6]):.2f}\
\
{metric_lines(metrics)}"
        buttons = [[InlineKeyboardButton("📈 Update Delivered Metrics", callback_data=f"admin_campaign_update_{order_id}")]]
        if order[8] == "active":
            buttons.append([InlineKeyboardButton("⏸ Pause Campaign", callback_data=f"admin_campaign_pause_{order_id}")])
        elif order[8] == "paused":
            buttons.append([InlineKeyboardButton("▶️ Resume Campaign", callback_data=f"admin_campaign_resume_{order_id}")])
        if order[8] in ("active", "paused"):
            buttons.append([InlineKeyboardButton("🏁 Complete Campaign", callback_data=f"admin_campaign_complete_{order_id}")])
        buttons += [[InlineKeyboardButton("⬅️ Campaigns", callback_data="admin_campaigns")],[InlineKeyboardButton("⬅️ Admin Dashboard", callback_data="admin_dashboard")]]
        await safe_edit_message_text(query, text, reply_markup=InlineKeyboardMarkup(buttons))
        return

    if query.data.startswith("admin_campaign_update_"):
        if update.effective_user.id != ADMIN_USER_ID:
            await safe_answer_callback(query, "❌ Admin access only.", show_alert=True)
            return
        order_id = query.data.replace("admin_campaign_update_", "", 1)
        if not get_order_by_id(order_id):
            await safe_answer_callback(query, "❌ Order not found.", show_alert=True)
            return
        context.user_data["admin_campaign_order"] = order_id
        context.user_data["waiting_admin_campaign_metrics"] = True
        await safe_edit_message_text(query, "📈 UPDATE DELIVERED METRICS\
\
Send six numbers in this exact order:\
Posts, Likes, Reposts, Bookmarks, Views, Comments\
\
Example:\
10, 500, 100, 80, 12000, 50")
        return

    if query.data.startswith("admin_campaign_pause_") or query.data.startswith("admin_campaign_resume_") or query.data.startswith("admin_campaign_complete_"):
        if update.effective_user.id != ADMIN_USER_ID:
            await safe_answer_callback(query, "❌ Admin access only.", show_alert=True)
            return
        if query.data.startswith("admin_campaign_pause_"):
            order_id = query.data.replace("admin_campaign_pause_", "", 1); new_status = "paused"; label = "⏸ PAUSED"
        elif query.data.startswith("admin_campaign_resume_"):
            order_id = query.data.replace("admin_campaign_resume_", "", 1); new_status = "active"; label = "▶️ ACTIVE"
        else:
            order_id = query.data.replace("admin_campaign_complete_", "", 1); new_status = "completed"; label = "🏁 COMPLETED"
        order = get_order_by_id(order_id)
        if not order:
            await safe_answer_callback(query, "❌ Order not found.", show_alert=True); return
        update_payment_status(order_id, "paid", new_status)
        try:
            await context.bot.send_message(chat_id=order[1], text=f"📊 CAMPAIGN UPDATE\
\
🆔 Order: {order_id}\
📊 Status: {label}\
\
Your VibeScale campaign status has been updated.")
        except Exception:
            pass
        await safe_edit_message_text(query, f"✅ CAMPAIGN UPDATED\
\
🆔 {order_id}\
📊 Status: {label}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Manage Campaign", callback_data=f"admin_campaign_view_{order_id}")],[InlineKeyboardButton("⬅️ Campaigns", callback_data="admin_campaigns")]]))
        return

    if query.data == "admin_orders":
        if update.effective_user.id != ADMIN_USER_ID:
            await query.answer("❌ Admin access only.", show_alert=True)
            return

        stats = get_admin_order_stats()
        rows = stats["recent_orders"]
        text = "📦 RECENT ORDERS\n\n"
        if not rows:
            text += "No orders yet."
        else:
            for row in rows:
                order_id, tg_user, plan, x_user, total, pay, status, created = row
                text += (
                    f"🆔 {order_id}\n"
                    f"👤 @{tg_user or 'No username'}\n"
                    f"🎯 {x_user}\n"
                    f"📦 {plan} — ${float(total):.2f}\n"
                    f"💳 {pay} | 📊 {status}\n"
                    f"🕒 {created[:10]}\n\n"
                )

        await safe_edit_message_text(
            query,
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Admin Dashboard", callback_data="admin_dashboard")],
            ])
        )
        return

    if query.data == "admin_support":
        if update.effective_user.id != ADMIN_USER_ID:
            await query.answer("❌ Admin access only.", show_alert=True)
            return

        tickets = get_admin_support_tickets(15)
        text = "🎫 SUPPORT TICKETS\n\n"
        if not tickets:
            text += "No support tickets yet."
        else:
            for t in tickets:
                ticket_id, user_id, tg_user, subject, message, status, created, updated = t
                text += (
                    f"🆔 {ticket_id}\n"
                    f"👤 @{tg_user or 'No username'} ({user_id})\n"
                    f"📌 {subject}\n"
                    f"💬 {message[:180]}\n"
                    f"📊 {status.upper()}\n"
                    f"🕒 {created[:16].replace('T', ' ')}\n\n"
                )

        await safe_edit_message_text(
            query,
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Admin Dashboard", callback_data="admin_dashboard")],
            ])
        )
        return

    if query.data == "admin_custom":
        if update.effective_user.id != ADMIN_USER_ID:
            await safe_answer_callback(query, "❌ Admin access only.", show_alert=True)
            return

        rows = get_admin_custom_quotes(10)
        text = "🧩 CUSTOM PLAN REQUESTS\n\n"
        keyboard = []
        if not rows:
            text += "No custom requests yet."
        else:
            for row in rows:
                (
                    request_id, user_id, tg_user, x_user, posts, likes,
                    reposts, bookmarks, views, comments, duration, notes,
                    status, created, quoted_price, admin_note, updated_at
                ) = row
                text += (
                    f"🆔 {request_id} | {status.replace('_', ' ').upper()}\n"
                    f"👤 @{tg_user or 'No username'}\n"
                    f"🎯 {x_user} | 📅 {duration} days\n"
                )
                if quoted_price is not None:
                    text += f"💵 Quote: ${float(quoted_price):,.2f}\n"
                text += "\n"
                keyboard.append([
                    InlineKeyboardButton(
                        f"🔎 Manage {request_id}",
                        callback_data=f"admin_custom_view_{request_id}"
                    )
                ])

        keyboard.append([InlineKeyboardButton("⬅️ Admin Dashboard", callback_data="admin_dashboard")])
        await safe_edit_message_text(
            query, text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if query.data.startswith("admin_custom_view_"):
        if update.effective_user.id != ADMIN_USER_ID:
            await safe_answer_callback(query, "❌ Admin access only.", show_alert=True)
            return

        request_id = query.data.replace("admin_custom_view_", "", 1)
        request = get_custom_quote(request_id)
        if not request:
            await safe_answer_callback(query, "❌ Request not found.", show_alert=True)
            return

        (
            req_id, user_id, tg_user, x_username, posts, likes, reposts,
            bookmarks, views, comments, duration, notes, status, created_at,
            quoted_price, admin_note, updated_at
        ) = request

        text = (
            "🧩 MANAGE CUSTOM REQUEST\n\n"
            f"🆔 {req_id}\n"
            f"👤 Customer: @{tg_user or 'No username'}\n"
            f"🆔 Telegram ID: {user_id}\n"
            f"🎯 X: {x_username}\n"
            f"📅 Duration: {duration} days\n\n"
            f"📝 Posts: {posts:,}\n"
            f"❤️ Likes: {likes:,}\n"
            f"🔁 Reposts: {reposts:,}\n"
            f"🔖 Bookmarks: {bookmarks:,}\n"
            f"👀 Views: {views:,}\n"
            f"💬 Comments: {comments:,}\n"
            f"📌 Notes: {notes}\n\n"
            f"📊 Status: {status.replace('_', ' ').upper()}\n"
        )
        if quoted_price is not None:
            text += f"💵 Current quote: ${float(quoted_price):,.2f}\n"
        if admin_note:
            text += f"🗒️ Admin note: {admin_note}\n"

        buttons = []
        if status in ("awaiting_quote", "quoted"):
            buttons.append([
                InlineKeyboardButton(
                    "💵 Send / Update Quote",
                    callback_data=f"admin_custom_quote_{req_id}"
                )
            ])
        if status in ("awaiting_quote", "quoted"):
            buttons.append([
                InlineKeyboardButton(
                    "❌ Reject Request",
                    callback_data=f"admin_custom_reject_{req_id}"
                )
            ])
        buttons.append([InlineKeyboardButton("⬅️ Custom Requests", callback_data="admin_custom")])
        buttons.append([InlineKeyboardButton("⬅️ Admin Dashboard", callback_data="admin_dashboard")])

        await safe_edit_message_text(
            query, text, reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if query.data.startswith("admin_custom_quote_"):
        if update.effective_user.id != ADMIN_USER_ID:
            await safe_answer_callback(query, "❌ Admin access only.", show_alert=True)
            return
        request_id = query.data.replace("admin_custom_quote_", "", 1)
        request = get_custom_quote(request_id)
        if not request or request[12] not in ("awaiting_quote", "quoted"):
            await safe_answer_callback(query, "❌ This request is no longer awaiting a quote.", show_alert=True)
            return

        context.user_data["admin_custom_quote_request"] = request_id
        context.user_data["waiting_admin_custom_quote"] = True
        await safe_edit_message_text(
            query,
            "💵 SET CUSTOM QUOTE\n\n"
            f"🆔 Request: {request_id}\n\n"
            "Send the price in USD.\n"
            "Example: 275 or 275.50\n\n"
            "You can optionally send a note after the price using a | separator.\n"
            "Example: 275 | Includes priority handling."
        )
        return

    if query.data.startswith("admin_custom_reject_"):
        if update.effective_user.id != ADMIN_USER_ID:
            await safe_answer_callback(query, "❌ Admin access only.", show_alert=True)
            return
        request_id = query.data.replace("admin_custom_reject_", "", 1)
        request = get_custom_quote(request_id)
        if not request:
            await safe_answer_callback(query, "❌ Request not found.", show_alert=True)
            return

        update_custom_quote(request_id, status="rejected", admin_note="Request rejected by VibeScale.")
        await safe_edit_message_text(
            query,
            f"❌ CUSTOM REQUEST REJECTED\n\n🆔 Request: {request_id}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Custom Requests", callback_data="admin_custom")],
                [InlineKeyboardButton("⬅️ Admin Dashboard", callback_data="admin_dashboard")],
            ])
        )
        await context.bot.send_message(
            chat_id=request[1],
            text=(
                "❌ CUSTOM PLAN REQUEST UPDATE\n\n"
                f"🆔 Request: {request_id}\n\n"
                "VibeScale reviewed your request and is unable to provide a quote for it at this time."
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧩 New Custom Plan", callback_data="custom")],
                [InlineKeyboardButton("🆘 Support", callback_data="support")],
            ])
        )
        return

    if query.data.startswith("admin_resolve_"):
        if update.effective_user.id != ADMIN_USER_ID:
            await query.answer("❌ Admin access only.", show_alert=True)
            return

        ticket_id = query.data.replace("admin_resolve_", "", 1)
        ticket = get_support_ticket(ticket_id)
        if not ticket:
            await query.answer("❌ Ticket not found.", show_alert=True)
            return

        update_support_ticket_status(ticket_id, "resolved")
        ticket_user_id = ticket[1]

        await safe_edit_message_text(
            query,
            f"✅ SUPPORT TICKET RESOLVED\n\n🆔 {ticket_id}\n📊 Status: RESOLVED",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Admin Dashboard", callback_data="admin_dashboard")]
            ])
        )

        try:
            await context.bot.send_message(
                chat_id=ticket_user_id,
                text=(
                    "✅ SUPPORT TICKET UPDATED\n\n"
                    f"🆔 Ticket: {ticket_id}\n"
                    "📊 Status: RESOLVED\n\n"
                    "Your VibeScale support ticket has been marked resolved."
                )
            )
        except Exception:
            pass
        return

    # HELP
    if query.data == "help":
        await safe_edit_message_text(query, 
            "ℹ️ How VibeScale Works\n\n"
            "1️⃣ Choose a plan\n"
            "2️⃣ Enter your X username\n"
            "3️⃣ Choose campaign duration\n"
            "4️⃣ Review your order\n"
            "5️⃣ Confirm your order\n"
            "6️⃣ Complete payment\n"
            "7️⃣ Track your campaign"
        )
        return


async def show_custom_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get("custom_plan", {})
    text = (
        "🧾 CUSTOM PLAN REVIEW\n\n"
        f"🎯 X username: {data.get('x_username')}\n"
        f"📅 Duration: {data.get('duration_days')} days\n\n"
        f"📝 Posts: {data.get('posts'):,}\n"
        f"❤️ Likes: {data.get('likes'):,}\n"
        f"🔁 Reposts: {data.get('reposts'):,}\n"
        f"🔖 Bookmarks: {data.get('bookmarks'):,}\n"
        f"👀 Views: {data.get('views'):,}\n"
        f"💬 Comments: {data.get('comments'):,}\n\n"
        f"📌 Notes: {data.get('notes', 'None')}\n\n"
        "Review your requirements. VibeScale will calculate and send your custom price after submission."
    )
    keyboard = [[InlineKeyboardButton("✅ Submit Request", callback_data="custom_submit")],
                [InlineKeyboardButton("✏️ Start Over", callback_data="custom")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_custom")]]
    if update.callback_query:
        await safe_edit_message_text(update.callback_query, text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


def parse_custom_number(value):
    value = value.strip().replace(",", "")
    if not value.isdigit():
        return None
    number = int(value)
    if number < 0:
        return None
    return number


# -------------------------
# TEXT HANDLER
# -------------------------

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ADMIN CAMPAIGN METRIC ENTRY
    if context.user_data.get("waiting_admin_campaign_metrics"):
        if update.effective_user.id != ADMIN_USER_ID:
            context.user_data.pop("waiting_admin_campaign_metrics", None)
            context.user_data.pop("admin_campaign_order", None)
            return
        raw = update.message.text.strip()
        parts = [p.strip().replace(",", "") for p in raw.split(",")]
        if len(parts) != 6 or any(not p.isdigit() for p in parts):
            await update.message.reply_text("❌ Invalid format. Send exactly 6 whole numbers separated by commas.\
\
Example: 10, 500, 100, 80, 12000, 50")
            return
        order_id = context.user_data.get("admin_campaign_order")
        metrics = {"posts":int(parts[0]),"likes":int(parts[1]),"reposts":int(parts[2]),"bookmarks":int(parts[3]),"views":int(parts[4]),"comments":int(parts[5])}
        changed = update_campaign_metrics(order_id, metrics)
        context.user_data.pop("waiting_admin_campaign_metrics", None)
        context.user_data.pop("admin_campaign_order", None)
        if not changed:
            await update.message.reply_text("❌ Campaign metrics could not be updated.")
            return
        await update.message.reply_text("✅ CAMPAIGN METRICS UPDATED\
\
" + metric_lines(get_campaign_metrics(order_id)), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛠️ Manage Campaign", callback_data=f"admin_campaign_view_{order_id}")],[InlineKeyboardButton("📊 Campaigns", callback_data="admin_campaigns")]]))
        order = get_order_by_id(order_id)
        if order:
            try:
                await context.bot.send_message(chat_id=order[1], text="📈 CAMPAIGN PROGRESS UPDATED\
\
🆔 Order: " + order_id + "\
\
" + metric_lines(get_campaign_metrics(order_id)), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📊 View Campaign", callback_data=f"campaign_view_{order_id}")]]))
            except Exception:
                pass
        return

    # ADMIN CUSTOM QUOTE ENTRY
    if context.user_data.get("waiting_admin_custom_quote"):
        if update.effective_user.id != ADMIN_USER_ID:
            context.user_data.pop("waiting_admin_custom_quote", None)
            context.user_data.pop("admin_custom_quote_request", None)
            return

        raw = update.message.text.strip()
        parts = raw.split("|", 1)
        price_text = parts[0].strip().replace("$", "").replace(",", "")
        note = parts[1].strip() if len(parts) > 1 else ""

        try:
            price = float(price_text)
            if price <= 0 or price > 1000000:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid quote. Send a positive USD amount.\n\n"
                "Example: 275 or 275.50\n"
                "Optional note: 275 | Includes priority handling."
            )
            return

        request_id = context.user_data.get("admin_custom_quote_request")
        request = get_custom_quote(request_id) if request_id else None
        if not request:
            context.user_data.pop("waiting_admin_custom_quote", None)
            context.user_data.pop("admin_custom_quote_request", None)
            await update.message.reply_text("❌ Custom request not found.")
            return

        admin_note = note or None
        update_custom_quote(
            request_id,
            status="quoted",
            quoted_price=price,
            admin_note=admin_note,
        )
        context.user_data.pop("waiting_admin_custom_quote", None)
        context.user_data.pop("admin_custom_quote_request", None)

        await update.message.reply_text(
            "✅ CUSTOM QUOTE SENT\n\n"
            f"🆔 Request: {request_id}\n"
            f"💵 Quote: ${price:,.2f}\n"
            "📊 Status: QUOTED"
        )

        customer_id = request[1]
        customer_text = (
            "💵 VIBESCALE CUSTOM QUOTE READY\n\n"
            f"🆔 Request: {request_id}\n"
            f"🎯 X: {request[3]}\n"
            f"📅 Duration: {request[10]} days\n"
            f"💵 Custom price: ${price:,.2f}\n\n"
            "VibeScale has reviewed your custom requirements and prepared a quote."
        )
        if admin_note:
            customer_text += f"\n\n📌 Note: {admin_note}"

        await context.bot.send_message(
            chat_id=customer_id,
            text=customer_text,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Accept & Pay", callback_data=f"custom_accept_{request_id}"),
                    InlineKeyboardButton("❌ Decline", callback_data=f"custom_decline_{request_id}"),
                ],
                [InlineKeyboardButton("📋 View Request", callback_data=f"custom_view_{request_id}")],
            ])
        )
        return

    # SUPPORT TICKET CREATION
    if context.user_data.get("support_state") == "subject":
        subject = update.message.text.strip()
        if len(subject) < 3:
            await update.message.reply_text("Please enter a slightly longer subject.")
            return

        context.user_data["support_subject"] = subject[:120]
        context.user_data["support_state"] = "message"

        await update.message.reply_text(
            "🆘 SUPPORT MESSAGE\n\n"
            "Now describe the issue in as much detail as useful.\n\n"
            "You can include your Order ID if the issue is related to a payment or campaign."
        )
        return

    if context.user_data.get("support_state") == "message":
        message = update.message.text.strip()
        if len(message) < 5:
            await update.message.reply_text("Please provide a little more detail so support can help.")
            return

        customer = update.effective_user
        ticket_id = f"VST-{random.randint(100000, 999999)}"
        subject = context.user_data.get("support_subject", "Support request")

        create_support_ticket(
            ticket_id=ticket_id,
            telegram_user_id=customer.id,
            telegram_username=customer.username,
            subject=subject,
            message=message[:2000],
        )

        context.user_data.pop("support_state", None)
        context.user_data.pop("support_subject", None)

        await update.message.reply_text(
            "✅ SUPPORT TICKET CREATED\n\n"
            f"🆔 Ticket: {ticket_id}\n"
            f"📌 Subject: {subject}\n\n"
            "Your request has been sent to VibeScale support. "
            "We'll review it and respond as soon as possible.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎫 My Tickets", callback_data="support_tickets")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="home")],
            ])
        )

        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=(
                "🆘 NEW VIBESCALE SUPPORT TICKET\n\n"
                f"🆔 Ticket: {ticket_id}\n"
                f"👤 Customer: @{customer.username or 'No username'}\n"
                f"🆔 Telegram ID: {customer.id}\n"
                f"📌 Subject: {subject}\n\n"
                f"💬 Message:\n{message[:3000]}\n\n"
                "📊 Status: OPEN"
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔒 Mark Resolved", callback_data=f"admin_resolve_{ticket_id}")],
                [InlineKeyboardButton("📋 Admin Dashboard", callback_data="admin_dashboard")],
            ])
        )
        return


    # CUSTOM PLAN BUILDER
    if context.user_data.get("waiting_custom_x_username"):
        username = update.message.text.strip()
        if not username.startswith("@"): username = "@" + username
        context.user_data.setdefault("custom_plan", {})["x_username"] = username
        context.user_data["waiting_custom_x_username"] = False
        context.user_data["waiting_custom_posts"] = True
        await update.message.reply_text("📝 POSTS\n\nHow many posts should VibeScale cover during the campaign?\n\nExample: 30")
        return

    custom_fields = [
        ("waiting_custom_posts", "posts", "📝 POSTS", "How many posts should VibeScale cover during the campaign?"),
        ("waiting_custom_likes", "likes", "❤️ LIKES", "How many likes do you want included?"),
        ("waiting_custom_reposts", "reposts", "🔁 REPOSTS", "How many reposts do you want included?"),
        ("waiting_custom_bookmarks", "bookmarks", "🔖 BOOKMARKS", "How many bookmarks do you want included?"),
        ("waiting_custom_views", "views", "👀 VIEWS", "How many views do you want included?"),
        ("waiting_custom_comments", "comments", "💬 COMMENTS", "How many comments do you want included?"),
    ]
    for state_key, data_key, title, prompt in custom_fields:
        if context.user_data.get(state_key):
            number = parse_custom_number(update.message.text)
            if number is None:
                await update.message.reply_text(f"❌ Please enter a whole number for {data_key}.\n\nExample: 1000")
                return
            context.user_data.setdefault("custom_plan", {})[data_key] = number
            context.user_data[state_key] = False
            next_map = {
                "posts": "waiting_custom_likes", "likes": "waiting_custom_reposts",
                "reposts": "waiting_custom_bookmarks", "bookmarks": "waiting_custom_views",
                "views": "waiting_custom_comments"
            }
            next_state = next_map.get(data_key)
            if next_state:
                context.user_data[next_state] = True
                next_prompt = {
                    "waiting_custom_likes": "❤️ LIKES\n\nHow many likes do you want included?",
                    "waiting_custom_reposts": "🔁 REPOSTS\n\nHow many reposts do you want included?",
                    "waiting_custom_bookmarks": "🔖 BOOKMARKS\n\nHow many bookmarks do you want included?",
                    "waiting_custom_views": "👀 VIEWS\n\nHow many views do you want included?",
                    "waiting_custom_comments": "💬 COMMENTS\n\nHow many comments do you want included?",
                }[next_state]
                await update.message.reply_text(next_prompt)
            else:
                await update.message.reply_text("📅 CAMPAIGN DURATION\n\nChoose how long you want the custom campaign to run.", reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("7 Days", callback_data="custom_duration_7")
                ], [
                    InlineKeyboardButton("14 Days", callback_data="custom_duration_14")
                ], [
                    InlineKeyboardButton("30 Days", callback_data="custom_duration_30")
                ], [
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_custom")
                ]]))
            return

    if context.user_data.get("waiting_custom_notes"):
        context.user_data.setdefault("custom_plan", {})["notes"] = update.message.text.strip() or "None"
        context.user_data["waiting_custom_notes"] = False
        await show_custom_review(update, context)
        return

    # PAYSTACK CUSTOMER EMAIL
    if context.user_data.get("waiting_for_paystack_email"):

        email = update.message.text.strip().lower()

        if not valid_email(email):
            await update.message.reply_text(
                "❌ That doesn't look like a valid email address.\n\n"
                "Please send a valid email, for example: customer@example.com"
            )
            return

        order_id = context.user_data.get("order_id")
        total_price = context.user_data.get("total_price")
        plan = context.user_data.get("selected_plan")
        x_username = context.user_data.get("x_username")

        if not order_id or total_price is None:
            context.user_data["waiting_for_paystack_email"] = False
            await update.message.reply_text(
                "❌ I couldn't find your active order. Please use /start to create a new order."
            )
            return

        await update.message.reply_text(
            "⏳ Getting the latest USD/NGN rate and creating your secure Paystack checkout..."
        )

        ngn_amount, fx_rate, fx_error = await calculate_ngn_amount(total_price)
        if fx_error:
            context.user_data["waiting_for_paystack_email"] = False
            await update.message.reply_text(
                "❌ CURRENCY CONVERSION ERROR\n\n"
                f"{fx_error}\n\n"
                "Please try again in a moment or choose Crypto payment instead."
            )
            return

        save_paystack_fx(order_id, ngn_amount, fx_rate, "CurrencyFreaks")

        checkout, error = await initialize_paystack_transaction(
            order_id=order_id,
            amount=total_price,
            email=email,
            plan=plan or "Unknown",
            x_username=x_username or "Unknown",
            ngn_amount=ngn_amount,
        )

        if error:
            context.user_data["waiting_for_paystack_email"] = False
            await update.message.reply_text(
                "❌ PAYSTACK CHECKOUT ERROR\n\n"
                f"{error}\n\n"
                "Please try again in a moment or choose Crypto payment instead."
            )
            return

        context.user_data["waiting_for_paystack_email"] = False
        context.user_data["paystack_reference"] = checkout["reference"]

        keyboard = [
            [InlineKeyboardButton(
                "💳 Open Secure Paystack Checkout",
                url=checkout["authorization_url"],
            )],
            [InlineKeyboardButton(
                "✅ I've Completed Payment",
                callback_data=f"paystack_verify_{order_id}",
            )],
            [InlineKeyboardButton(
                "⬅️ Payment Methods",
                callback_data="payment_start",
            )],
        ]

        await update.message.reply_text(
            "💳 PAYSTACK CHECKOUT READY\n\n"
            f"🆔 Order: {order_id}\n"
            f"💵 VibeScale price: ${total_price:.2f} USD\n"
             f"🇳🇬 Paystack amount: ₦{int(ngn_amount):,} NGN\n"
            f"📈 FX rate: ₦{fx_rate:,.4f} per $1\n"
            f"📧 Email: {email}\n\n"
            "1️⃣ Open the secure Paystack checkout.\n"
            "2️⃣ Pay by card or an available bank payment method.\n"
            "3️⃣ Return here and tap 'I've Completed Payment'.\n\n"
            "🔒 VibeScale will verify the payment directly with Paystack before activating your order.\n\n"
            f"Reference: {checkout['reference']}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # CRYPTO TRANSACTION HASH SUBMISSION
    if context.user_data.get("waiting_for_tx_hash"):

        tx_hash = update.message.text.strip()
        order_id = context.user_data.get("order_id")
        total_price = context.user_data.get("total_price")
        plan = context.user_data.get("selected_plan")
        username = context.user_data.get("x_username")
        crypto_key = context.user_data.get("payment_crypto_key")
        crypto = CRYPTO_ADDRESSES.get(crypto_key)

        if not order_id or not crypto:
            context.user_data["waiting_for_tx_hash"] = False
            await update.message.reply_text(
                "❌ I couldn't match this payment to an active order.\n\n"
                "Please use /start to create or reopen an order."
            )
            return

        # Save the submitted hash. Payment remains pending until verification.
        save_payment_submission(
            order_id=order_id,
            payment_network=crypto["network"],
            transaction_hash=tx_hash,
        )

        customer = update.effective_user
        customer_name = customer.full_name
        customer_username = customer.username or "No username"

        admin_message = (
            "🚨 VIBESCALE PAYMENT VERIFICATION REQUEST\n\n"
            f"🆔 Order ID: {order_id}\n"
            f"👤 Customer: {customer_name}\n"
            f"📱 Telegram: @{customer_username}\n"
            f"🎯 X username: {username}\n\n"
            f"📦 Plan: {plan}\n"
            f"💵 Amount: ${total_price:.2f}\n"
            f"🌐 Network: {crypto['network']}\n"
            f"🔗 TXID: {tx_hash}\n\n"
            "💳 Payment: PENDING VERIFICATION\n\n"
            "Verify the transaction on the blockchain before marking "
            "the order as PAID."
        )

        admin_keyboard = [
            [InlineKeyboardButton(
                "✅ Verify & Mark PAID",
                callback_data=f"admin_verify_{order_id}",
            )],
            [InlineKeyboardButton(
                "❌ Reject Payment",
                callback_data=f"admin_reject_{order_id}",
            )],
        ]

        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=admin_message,
            reply_markup=InlineKeyboardMarkup(admin_keyboard),
        )

        context.user_data["waiting_for_tx_hash"] = False

        await update.message.reply_text(
            "⏳ PAYMENT SUBMITTED\n\n"
            f"🆔 Order: {order_id}\n"
            f"💵 Amount: ${total_price:.2f}\n"
            f"🌐 Network: {crypto['network']}\n\n"
            "Your transaction hash has been submitted to VibeScale.\n\n"
            "✅ We will verify the transaction before activating your order.\n\n"
            "Please keep your TXID for your records."
        )
        return

    if context.user_data.get("waiting_for_username"):

        username = update.message.text.strip()

        if not username.startswith("@"):
            username = "@" + username

        context.user_data["x_username"] = username
        context.user_data["waiting_for_username"] = False

        plan = context.user_data["selected_plan"]
        weekly_price = context.user_data["weekly_price"]

        keyboard = [
            [
                InlineKeyboardButton(
                    "7 Days",
                    callback_data="duration_7"
                )
            ],
            [
                InlineKeyboardButton(
                    "14 Days",
                    callback_data="duration_14"
                )
            ],
            [
                InlineKeyboardButton(
                    "30 Days",
                    callback_data="duration_30"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="cancel_order"
                )
            ],
        ]

        await update.message.reply_text(
            f"✅ X username saved: {username}\n\n"
            f"📦 Plan: {plan}\n"
            f"💵 Weekly price: ${weekly_price:.2f}\n\n"
            "How long would you like to run the campaign?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


# -------------------------
# ADMIN DASHBOARD COMMAND
# -------------------------

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Admin access only.")
        return

    stats = get_admin_order_stats()
    tickets = get_admin_support_tickets(10)
    custom_quotes = get_admin_custom_quotes(10)

    await update.message.reply_text(
        "🛠️ VIBESCALE ADMIN DASHBOARD\n\n"
        f"📦 Total orders: {stats['total_orders']}\n"
        f"💳 Paid orders: {stats['paid_orders']}\n"
        f"📈 Active campaigns: {stats['active_orders']}\n"
        f"💰 Paid order value: ${float(stats['paid_value']):,.2f}\n"
        f"🎫 Open tickets: {sum(1 for t in tickets if t[5] == 'open')}\n"
        f"🧩 Recent custom requests: {len(custom_quotes)}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 Recent Orders", callback_data="admin_orders")],
            [InlineKeyboardButton("🎫 Support Tickets", callback_data="admin_support")],
            [InlineKeyboardButton("🧩 Custom Requests", callback_data="admin_custom")],
            [InlineKeyboardButton("🔄 Refresh", callback_data="admin_dashboard")],
        ])
    )

# -------------------------
# SHOW TELEGRAM USER ID
# -------------------------

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    await update.message.reply_text(
        f"🆔 Your Telegram User ID:\n\n"
        f"`{user_id}`",
        parse_mode="Markdown",
    )

# -------------------------
# MAIN
# -------------------------

def main():

    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN is not set.")
        return

    if not PAYSTACK_SECRET_KEY:
        print("WARNING: PAYSTACK_SECRET_KEY is not set. Bank/Card payments will not work.")
    if not CURRENCYFREAKS_API_KEY:
        print("WARNING: CURRENCYFREAKS_API_KEY is not set. NGN conversion will not work.")

    # Create the database/table if it doesn't exist
    initialize_database()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(paystack_verify_handler, pattern=r"^paystack_verify_[A-Za-z0-9-]+$"))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(telegram_error_handler)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    print("VibeScale bot is running...")

    app.run_polling()


if __name__ == "__main__":
    main()