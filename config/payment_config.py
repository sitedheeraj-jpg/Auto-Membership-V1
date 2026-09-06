import os
from dataclasses import dataclass

from .settings import settings


@dataclass(frozen=True)
class PaymentConfig:
    # VC Payment (UPI) Details
    upi_id: str = os.environ.get("UPI_ID", "Paytm.s1dw5n0@pty")
    upi_payee_name: str = os.environ.get("UPI_PAYEE_NAME", "Dharmendra Madal")
    paytm_mid: str = os.environ.get("PAYTM_MID", "")
    upi_note: str = os.environ.get("UPI_NOTE", "VC Payment")

    # Auto-Payment Verification (VC Payment API)
    payment_api_url: str = os.environ.get(
        "PAYMENT_API_URL", "https://vcapi.vcstore.site/payment_api.php"
    )
    payment_api_key: str = os.environ.get("PAYMENT_API_KEY") or os.environ.get(
        "PAYTM_MID", ""
    )
    payment_verify_interval: int = int(
        os.environ.get("PAYMENT_VERIFY_INTERVAL", "120")
    )
    # The supplied configuration used 15 minutes. Set this to 20 in Railway
    # if you want the longer window mentioned in the original request.
    payment_max_minutes: int = int(os.environ.get("PAYMENT_MAX_MINUTES", "15"))
    amount_tolerance: float = float(os.environ.get("AMOUNT_TOLERANCE", "2"))
    payment_log_channel_id: int = int(
        os.environ.get("PAYMENT_LOG_CHANNEL_ID", "-1004330990257")
    )


payment_config = PaymentConfig()