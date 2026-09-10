"""
===============================================================
FraudShield - XGBoost Fraud Inference
===============================================================

Purpose:
    Load the trained FraudShield XGBoost model and predict the
    fraud probability for a single transaction.

Model artifacts:
    ml/fraud/models/fraud_xgboost_model.json
    ml/fraud/models/fraud_categorical_encoder.pkl
    ml/fraud/models/training_metadata.json

IMPORTANT:
    This module returns ML fraud probability only.

    It does NOT calculate the final FraudShield 0-100 risk score.

    The final risk engine will later combine:
        - ML fraud probability
        - deterministic rules
        - behavioral signals
        - anomaly detection
        - transaction history
        - explanations

===============================================================
"""

from pathlib import Path
import json
import joblib
import numpy as np
import pandas as pd
import xgboost as xgb


# ===============================================================
# PATH CONFIGURATION
# ===============================================================

BASE_DIR = Path(__file__).resolve().parents[2]

MODEL_DIR = BASE_DIR / "ml" / "fraud" / "models"

MODEL_PATH = MODEL_DIR / "fraud_xgboost_model.json"
ENCODER_PATH = MODEL_DIR / "fraud_categorical_encoder.pkl"
METADATA_PATH = MODEL_DIR / "training_metadata.json"


# ===============================================================
# FRAUD INFERENCE SERVICE
# ===============================================================

class FraudInferenceService:

    def __init__(self):

        self.model = None
        self.encoder = None
        self.metadata = None

        self.numeric_features = []
        self.categorical_features = []
        self.model_features = []

        self.load_artifacts()

    # ===========================================================
    # LOAD TRAINED ARTIFACTS
    # ===========================================================

    def load_artifacts(self):

        print("Loading FraudShield model...")

        # -------------------------------------------------------
        # Validate model files
        # -------------------------------------------------------

        if not MODEL_PATH.exists():

            raise FileNotFoundError(
                f"\nFraud model not found:\n{MODEL_PATH}"
            )

        if not ENCODER_PATH.exists():

            raise FileNotFoundError(
                f"\nCategorical encoder not found:\n"
                f"{ENCODER_PATH}"
            )

        if not METADATA_PATH.exists():

            raise FileNotFoundError(
                f"\nTraining metadata not found:\n"
                f"{METADATA_PATH}"
            )

        # -------------------------------------------------------
        # Load XGBoost model
        # -------------------------------------------------------

        self.model = xgb.XGBClassifier()

        self.model.load_model(
            str(MODEL_PATH)
        )

        # -------------------------------------------------------
        # Load categorical encoder
        # -------------------------------------------------------

        self.encoder = joblib.load(
            ENCODER_PATH
        )

        # -------------------------------------------------------
        # Load metadata
        # -------------------------------------------------------

        with open(
            METADATA_PATH,
            "r",
            encoding="utf-8"
        ) as file:

            self.metadata = json.load(file)

        # -------------------------------------------------------
        # Read feature lists
        # -------------------------------------------------------

        self.numeric_features = (
            self.metadata.get(
                "numeric_features",
                []
            )
        )

        self.categorical_features = (
            self.metadata.get(
                "categorical_features",
                []
            )
        )

        # -------------------------------------------------------
        # IMPORTANT:
        #
        # The previous implementation expected:
        #
        #     metadata["model_features"]
        #
        # but your training metadata does not contain that key.
        #
        # Therefore we reconstruct it.
        # -------------------------------------------------------

        metadata_model_features = (
            self.metadata.get(
                "model_features"
            )
        )

        if metadata_model_features:

            self.model_features = list(
                metadata_model_features
            )

        else:

            self.model_features = list(
                self.numeric_features
                +
                self.categorical_features
            )

        # -------------------------------------------------------
        # Remove duplicate feature names
        # -------------------------------------------------------

        self.model_features = list(
            dict.fromkeys(
                self.model_features
            )
        )

        # -------------------------------------------------------
        # MOST IMPORTANT FIX:
        #
        # XGBoost itself stores the exact feature order used
        # during training.
        #
        # We will use that order for inference.
        # -------------------------------------------------------

        trained_feature_names = (
            self.model
            .get_booster()
            .feature_names
        )

        if not trained_feature_names:

            raise ValueError(
                "\nThe trained XGBoost model does not contain "
                "feature names."
            )

        self.model_features = list(
            trained_feature_names
        )

        # -------------------------------------------------------
        # Diagnostics
        # -------------------------------------------------------

        print(
            "FraudShield model loaded successfully."
        )

        print(
            f"Numeric features: "
            f"{len(self.numeric_features)}"
        )

        print(
            f"Categorical features: "
            f"{len(self.categorical_features)}"
        )

        print(
            f"Total model features: "
            f"{len(self.model_features)}"
        )

        print(
            "Categorical columns:"
        )

        for feature in self.categorical_features:

            print(
                f" - {feature}"
            )

        # -------------------------------------------------------
        # Validate expected model size
        # -------------------------------------------------------

        if len(self.model_features) != 116:

            print(
                "\nWARNING:"
            )

            print(
                "The trained model does not contain "
                "116 features."
            )

            print(
                f"Actual model feature count: "
                f"{len(self.model_features)}"
            )


    # ===========================================================
    # FEATURE ENGINEERING
    # ===========================================================

    def prepare_transaction(
        self,
        transaction
    ):
        """
        Convert a transaction dictionary into the exact
        feature structure expected by the trained model.

        The final DataFrame column order is taken directly
        from the trained XGBoost model.
        """

        # -------------------------------------------------------
        # Create one-row DataFrame
        # -------------------------------------------------------

        df = pd.DataFrame(
            [transaction]
        )

        # =======================================================
        # AMOUNT FEATURES
        # =======================================================

        if "TransactionAmt" in df.columns:

            amount = pd.to_numeric(
                df["TransactionAmt"],
                errors="coerce"
            )

        else:

            amount = pd.Series(
                [np.nan],
                index=df.index
            )

            df["TransactionAmt"] = np.nan


        # -------------------------------------------------------
        # Log amount
        # -------------------------------------------------------

        df["amount_log"] = np.log1p(
            amount.clip(
                lower=0
            )
        )


        # -------------------------------------------------------
        # Decimal amount flag
        # -------------------------------------------------------

        df["amount_has_decimal"] = (
            (amount % 1) != 0
        ).astype(int)


        # -------------------------------------------------------
        # Amount bucket
        # -------------------------------------------------------

        df["amount_bucket"] = pd.cut(
            amount,
            bins=[
                -np.inf,
                10,
                50,
                100,
                250,
                500,
                1000,
                5000,
                np.inf
            ],
            labels=False
        )


        # =======================================================
        # TRANSACTION TIME FEATURES
        # =======================================================

        if "TransactionDT" in df.columns:

            transaction_dt = pd.to_numeric(
                df["TransactionDT"],
                errors="coerce"
            )

        else:

            transaction_dt = pd.Series(
                [np.nan],
                index=df.index
            )

            df["TransactionDT"] = np.nan


        # -------------------------------------------------------
        # Day
        # -------------------------------------------------------

        df["transaction_day"] = (
            transaction_dt // 86400
        )


        # -------------------------------------------------------
        # Second of day
        # -------------------------------------------------------

        df["transaction_second_of_day"] = (
            transaction_dt % 86400
        )


        # -------------------------------------------------------
        # Hour
        # -------------------------------------------------------

        df["transaction_hour"] = (
            df["transaction_second_of_day"]
            // 3600
        )


        # -------------------------------------------------------
        # Weekday proxy
        # -------------------------------------------------------

        df["transaction_weekday_proxy"] = (
            df["transaction_day"] % 7
        )


        # -------------------------------------------------------
        # Night transaction
        # -------------------------------------------------------

        df["is_night_transaction"] = (
            (df["transaction_hour"] < 6)
            |
            (df["transaction_hour"] >= 22)
        ).astype(int)


        # =======================================================
        # CARD FEATURES
        # =======================================================

        card_columns = [
            "card1",
            "card2",
            "card3",
            "card4",
            "card5",
            "card6"
        ]

        existing_card_columns = [
            column
            for column in card_columns
            if column in df.columns
        ]

        if existing_card_columns:

            df["card_missing_count"] = (
                df[
                    existing_card_columns
                ]
                .isna()
                .sum(axis=1)
            )

        else:

            df["card_missing_count"] = 0


        # =======================================================
        # ADDRESS FEATURES
        # =======================================================

        address_columns = [
            "addr1",
            "addr2"
        ]

        existing_address_columns = [
            column
            for column in address_columns
            if column in df.columns
        ]

        if existing_address_columns:

            df["address_missing_count"] = (
                df[
                    existing_address_columns
                ]
                .isna()
                .sum(axis=1)
            )

        else:

            df["address_missing_count"] = 0


        # =======================================================
        # DISTANCE FEATURES
        # =======================================================

        distance_columns = [
            "dist1",
            "dist2"
        ]

        existing_distance_columns = [
            column
            for column in distance_columns
            if column in df.columns
        ]

        if existing_distance_columns:

            df["distance_available_count"] = (
                df[
                    existing_distance_columns
                ]
                .notna()
                .sum(axis=1)
            )

        else:

            df["distance_available_count"] = 0


        # =======================================================
        # EMAIL FEATURES
        # =======================================================

        if (
            "P_emaildomain" in df.columns
            and
            "R_emaildomain" in df.columns
        ):

            df["email_domain_match"] = (
                df["P_emaildomain"]
                .fillna("")
                ==
                df["R_emaildomain"]
                .fillna("")
            ).astype(int)

        else:

            df["email_domain_match"] = 0


        # -------------------------------------------------------
        # Email missing count
        # -------------------------------------------------------

        email_columns = [
            "P_emaildomain",
            "R_emaildomain"
        ]

        existing_email_columns = [
            column
            for column in email_columns
            if column in df.columns
        ]

        if existing_email_columns:

            df["email_missing_count"] = (
                df[
                    existing_email_columns
                ]
                .isna()
                .sum(axis=1)
            )

        else:

            df["email_missing_count"] = 0


        # =======================================================
        # IDENTITY FEATURES
        # =======================================================

        identity_columns = [
            "DeviceType",
            "DeviceInfo"
        ]

        existing_identity_columns = [
            column
            for column in identity_columns
            if column in df.columns
        ]

        if existing_identity_columns:

            df["identity_missing_count"] = (
                df[
                    existing_identity_columns
                ]
                .isna()
                .sum(axis=1)
            )

            df["identity_available"] = (
                df[
                    existing_identity_columns
                ]
                .notna()
                .any(axis=1)
                .astype(int)
            )

        else:

            df["identity_missing_count"] = 0

            df["identity_available"] = 0


        # =======================================================
        # DEVICE FLAGS
        # =======================================================

        if "DeviceType" in df.columns:

            df["has_device_type"] = (
                df["DeviceType"]
                .notna()
                .astype(int)
            )

        else:

            df["has_device_type"] = 0


        if "DeviceInfo" in df.columns:

            df["has_device_info"] = (
                df["DeviceInfo"]
                .notna()
                .astype(int)
            )

        else:

            df["has_device_info"] = 0


        # =======================================================
        # MISSINGNESS FEATURES
        # =======================================================

        df["total_missing_features"] = (
            df.isna()
            .sum(axis=1)
        )

        df["missing_feature_ratio"] = (
            df.isna()
            .mean(axis=1)
        )


        # =======================================================
        # ENSURE ALL TRAINING FEATURES EXIST
        # =======================================================

        # We use the exact feature names stored in the trained
        # XGBoost model.

        missing_features = [
            feature
            for feature in self.model_features
            if feature not in df.columns
        ]

        if missing_features:

            # Add all missing columns at once instead of
            # inserting them one-by-one.
            #
            # This also avoids pandas' DataFrame fragmentation
            # warning.

            missing_data = pd.DataFrame(
                np.nan,
                index=df.index,
                columns=missing_features
            )

            df = pd.concat(
                [
                    df,
                    missing_data
                ],
                axis=1
            )


        # =======================================================
        # KEEP REQUIRED FEATURES
        # =======================================================

        df = df[
            self.model_features
        ].copy()


        # =======================================================
        # CATEGORICAL ENCODING
        # =======================================================

        if self.categorical_features:

            categorical_data = df[
                self.categorical_features
            ].copy()

            # Match the dtype behavior used during training.

            categorical_data = (
                categorical_data
                .astype("string")
            )

            encoded_values = (
                self.encoder.transform(
                    categorical_data
                )
            )

            encoded_df = pd.DataFrame(
                encoded_values,
                columns=self.categorical_features,
                index=df.index
            )

            for feature in self.categorical_features:

                df[feature] = (
                    encoded_df[feature]
                )


        # =======================================================
        # NUMERIC FEATURE CONVERSION
        # =======================================================

        for feature in self.numeric_features:

            if feature in df.columns:

                df[feature] = pd.to_numeric(
                    df[feature],
                    errors="coerce"
                )


        # =======================================================
        # FINAL EXACT MODEL ORDER
        # =======================================================

        df = df[
            self.model_features
        ].copy()


        # =======================================================
        # FINAL VALIDATION
        # =======================================================

        actual_features = list(
            df.columns
        )

        expected_features = list(
            self.model_features
        )

        if actual_features != expected_features:

            raise ValueError(
                "\nFinal feature order mismatch.\n"
                f"Expected:\n{expected_features}\n"
                f"Received:\n{actual_features}"
            )


        if df.shape[1] != len(
            self.model_features
        ):

            raise ValueError(
                "\nFeature count mismatch.\n"
                f"Expected: "
                f"{len(self.model_features)}\n"
                f"Received: "
                f"{df.shape[1]}"
            )


        return df


    # ===========================================================
    # FRAUD PREDICTION
    # ===========================================================

    def predict(
        self,
        transaction
    ):
        """
        Predict fraud probability for one transaction.
        """

        # -------------------------------------------------------
        # Prepare features
        # -------------------------------------------------------

        features = (
            self.prepare_transaction(
                transaction
            )
        )

        # -------------------------------------------------------
        # Predict probability
        # -------------------------------------------------------

        probability = (
            self.model
            .predict_proba(
                features
            )[0][1]
        )

        # -------------------------------------------------------
        # Clamp probability
        # -------------------------------------------------------

        probability = float(
            np.clip(
                probability,
                0.0,
                1.0
            )
        )

        # -------------------------------------------------------
        # Return structured result
        # -------------------------------------------------------

        return {

            "fraud_probability":
                probability,

            "fraud_percentage":
                round(
                    probability * 100,
                    2
                ),

            "model":
                "FraudShield-XGBoost"
        }


# ===============================================================
# SINGLETON SERVICE
# ===============================================================

fraud_service = None


def get_fraud_service():

    global fraud_service

    if fraud_service is None:

        fraud_service = (
            FraudInferenceService()
        )

    return fraud_service


# ===============================================================
# PUBLIC PREDICTION FUNCTION
# ===============================================================

def predict_fraud(
    transaction
):
    """
    Public helper function used by the rest of the application.
    """

    service = get_fraud_service()

    return service.predict(
        transaction
    )


# ===============================================================
# TEST TRANSACTION
# ===============================================================

if __name__ == "__main__":

    print()
    print("=" * 70)
    print("FRAUDSHIELD INFERENCE TEST")
    print("=" * 70)
    print()

    # -----------------------------------------------------------
    # Synthetic transaction for testing the inference pipeline.
    #
    # This is NOT a real FinTrust transaction and is NOT being
    # claimed to represent a genuine/fraudulent transaction.
    # -----------------------------------------------------------

    sample_transaction = {

        "TransactionAmt": 250.50,

        "ProductCD": "W",

        "card1": 10000,
        "card2": 111,
        "card3": 150,
        "card4": "visa",
        "card5": 226,
        "card6": "debit",

        "addr1": 100,
        "addr2": 87,

        "dist1": 10,
        "dist2": np.nan,

        "P_emaildomain": "gmail.com",
        "R_emaildomain": "gmail.com",

        "DeviceType": "desktop",
        "DeviceInfo": "Windows",

        "TransactionDT": 15000000
    }


    try:

        result = predict_fraud(
            sample_transaction
        )

        print()
        print("Prediction:")
        print(
            json.dumps(
                result,
                indent=4
            )
        )

        print()
        print(
            "Fraud probability:",
            result[
                "fraud_probability"
            ]
        )

        print(
            "Fraud percentage:",
            result[
                "fraud_percentage"
            ],
            "%"
        )

        print()
        print("=" * 70)
        print("INFERENCE TEST COMPLETED SUCCESSFULLY")
        print("=" * 70)
        print()


    except Exception as error:

        print()
        print("=" * 70)
        print("INFERENCE TEST FAILED")
        print("=" * 70)
        print()

        print(
            "ERROR:"
        )

        print(
            str(error)
        )

        print()

        raise