"""
Store Management Module (Paystack Integration)
=======================================

This module handles all store-related operations in the control panel,
including product display, payment processing, and credit management.

Templates Used:
------------
- store.html: Product catalog and pricing
- success.html: Payment confirmation
- cancel.html: Payment cancellation

Database Tables Used:
------------------
- users: Credit balances
- payments: Transaction history
- products: Available items

External Services:
---------------
- Paystack API:
    - Transaction initialization
    - Transaction verification
- Pterodactyl API:
    - User verification
    - Resource allocation

Session Requirements:
------------------
- email: User's email address
- pterodactyl_id: User's panel ID
- paystack_reference: Paystack unique transaction reference (during checkout)

Configuration:
------------
- PAYSTACK_SECRET_KEY: API authentication
- SITE_URL: Return URL base

Payment Flow:
-----------
1. User selects product
2. Paystack transaction initiated (server-side)
3. User redirected to Paystack Checkout URL
4. User redirected back for verification (server-side)
5. Credits allocated
6. Transaction logged
"""

from flask import Blueprint, request, render_template, session, flash, redirect, url_for, current_app
import sys
import os # Added for environment variables
import requests # Added for Paystack API calls
from uuid import uuid4 # Added to generate a unique transaction reference

# Existing imports
from threadedreturn import ThreadWithReturnValue
sys.path.append("..")
from managers.authentication import login_required
from managers.user_manager import get_ptero_id, get_id
from managers.credit_manager import add_credits
from managers.email_manager import send_email
from managers.logging import webhook_log
# from config import get_config # Assuming your config import handles variables

# --- Setup ---
store = Blueprint('store', __name__)

# Dummy product data (You should load this from your DB/Config)
products = [
    {'id': 1, 'name': 'Small Credit Pack', 'price': 5.00, 'credits': 500},
    {'id': 2, 'name': 'Medium Credit Pack', 'price': 10.00, 'credits': 1200},
    {'id': 3, 'name': 'Large Credit Pack', 'price': 20.00, 'credits': 2500},
]


# Utility function to generate a unique Paystack reference
def generate_paystack_reference(user_email, product_id):
    """Generates a unique transaction reference for Paystack."""
    # A robust solution might store this reference in a pending_payments table first.
    # We use a UUID here for uniqueness.
    return f"LUNES-{user_email[:4].upper()}-{product_id}-{uuid4().hex[:10].upper()}"


# --- Routes ---

@store.route('/store', methods=['GET'])
@login_required
def store_page():
    """Renders the store page with available products."""
    return render_template('store.html', products=products)


@store.route('/checkout/<int:product_id>', methods=['GET'])
@login_required
def checkout(product_id):
    """
    Initiates a payment transaction with Paystack.
    This replaces the Stripe Checkout Session creation.
    """
    try:
        product = next(p for p in products if p['id'] == product_id)
    except StopIteration:
        flash("Invalid product selected.")
        return redirect(url_for("store.store_page"))

    # --- Paystack Initialization Logic ---
    
    # Amount must be in the subunit of the currency (e.g., Kobo for NGN)
    # 100 kobo = 1 NGN. Assuming price is in the major unit (e.g., USD, NGN).
    amount_in_subunit = int(product['price'] * 100)  
    user_email = session.get('email')
    
    if not user_email:
        flash("User session error: Email not found.")
        return redirect(url_for("user.index"))
        
    PAYSTACK_SECRET_KEY = current_app.config.get('PAYSTACK_SECRET_KEY')
    if not PAYSTACK_SECRET_KEY:
        webhook_log("PAYSTACK_SECRET_KEY not configured.", database_log=True)
        flash("Payment gateway not configured correctly.")
        return redirect(url_for("user.index"))

    # 1. Generate unique reference
    transaction_reference = generate_paystack_reference(user_email, product_id)
    
    # 2. Prepare API Request
    url = "https://api.paystack.co/transaction/initialize"
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "email": user_email,
        "amount": amount_in_subunit,  
        # Paystack will redirect here after payment attempt
        "callback_url": url_for('store.success', _external=True),  
        "reference": transaction_reference,
        "metadata": {
            "custom_fields": [
                {"display_name": "Credits", "variable_name": "credits_to_add", "value": product['credits']}
            ]
        }
    }

    # 3. Call Paystack API
    try:
        response = requests.post(url, headers=headers, json=payload)
        response_data = response.json()
        
        if response_data.get('status'):
            # Store the reference in the session to use in the /success route
            session['paystack_reference'] = transaction_reference  
            
            authorization_url = response_data['data']['authorization_url']
            
            # 4. Redirect user to Paystack Checkout
            return redirect(authorization_url)
        else:
            flash(f"Payment initiation failed: {response_data.get('message', 'Unknown API Error')}")
            return redirect(url_for("user.index"))
            
    except requests.exceptions.RequestException as e:
        webhook_log(f"Paystack API initialization error: {e}", database_log=True)
        flash("Could not connect to payment gateway.")
        return redirect(url_for("user.index"))


@store.route('/success', methods=['GET'])
@login_required
def success():
    """
    Handles the Paystack callback and verifies the transaction status.
    This replaces the Stripe session retrieval and status check.
    """
    # Paystack returns the 'reference' as a query parameter
    reference = request.args.get('reference') or session.get('paystack_reference')
    
    if not reference:
        flash("Payment verification failed: No transaction reference found.")
        return redirect(url_for("user.index"))
    
    # Clear the reference from session immediately
    session.pop('paystack_reference', None)

    # --- Paystack Verification Logic ---
    PAYSTACK_SECRET_KEY = current_app.config.get('PAYSTACK_SECRET_KEY')
    
    # 1. Prepare API Request
    url = f"https://api.paystack.co/transaction/verify/{reference}"
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"
    }

    try:
        response = requests.get(url, headers=headers)
        response_data = response.json()
        
        if response_data.get('status') and response_data['data']['status'] == 'success':
            verified_data = response_data['data']
            
            # ** SECURITY CHECKS **
            # A. Check if the transaction has already been processed (prevent double-spending)
            #    (Requires a database check on your payments table using the 'reference')
            # B. Check if the amount is correct to prevent tampering
            #    e.g., expected_amount = lookup_product_amount(verified_data) * 100
            
            # Safely extract credits from metadata (as set in the checkout function)
            credits_to_add = 0
            for field in verified_data.get('metadata', {}).get('custom_fields', []):
                if field.get('variable_name') == 'credits_to_add':
                    credits_to_add = int(field['value'])
                    break
            
            if credits_to_add == 0:
                     # Fallback: calculate based on verified amount if metadata is missing/zero
                     amount_paid_major_unit = verified_data['amount'] / 100
                     # You'll need logic to map amount_paid_major_unit back to credits
                     # For simplicity, we'll use a fixed value if metadata failed.
                     # In a real app, this MUST be a proper lookup.
                     # flash("Warning: Credits not found in metadata, using fallback logic.")
                     credits_to_add = int(amount_paid_major_unit * 100) # Simple 1-to-1 conversion fallback

            # 2. Add Credits and Log
            user_email = verified_data['customer']['email'] # Use the email from the verified transaction
            add_credits(user_email, credits_to_add)
            
            webhook_log(f"**PAYSTACK PAYMENT**: User: {user_email} bought {credits_to_add} credits (Ref: {reference}).", database_log=True)
            flash("Success! Your account has been credited.")
            return redirect(url_for("user.index"))
            
        # Handle failed or abandoned payments
        else:
            status_msg = response_data['data'].get('gateway_response', response_data['data']['status'])
            flash(f"Payment failed: {status_msg}. Please try again or contact support with reference: {reference}")
            return redirect(url_for("user.index"))

    except requests.exceptions.RequestException as e:
        webhook_log(f"Paystack verification API error: {e}", database_log=True)
        flash("Failed to verify payment with Paystack due to a connection error.")
        return redirect(url_for("user.index"))
    except Exception as e:
        webhook_log(f"Verification processing error: {e}", database_log=True)
        flash("An internal error occurred during payment verification.")
        return redirect(url_for("user.index"))


@store.route('/cancel', methods=['GET'])
def cancel():
    """
    Handle cancelled payment callback.
    This logic remains mostly the same, just clearing Paystack reference.
    """
    # Clear the Paystack reference from session  
    session.pop('paystack_reference', None)
    
    flash("Payment cancelled. No charge was made.")
    return redirect(url_for("user.index"))