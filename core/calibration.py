import pandas as pd
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
import joblib
import os

class FillProbabilityCalibrator:
    """
    Empirical calibration for maker order fill probability using Logistic Regression.
    Features: entry_price, signal_score, model_edge, latency_detected
    Target: fill_success (1/0)
    """
    def __init__(self, model_path: str = "data/fill_calibrator.pkl"):
        self.model_path = model_path
        self.model = None
        self.scaler = StandardScaler()
        
        if os.path.exists(self.model_path):
            try:
                state = joblib.load(self.model_path)
                self.model = state["model"]
                self.scaler = state["scaler"]
            except Exception as e:
                print(f"Failed to load calibration model: {e}")
                self.model = LogisticRegression(class_weight="balanced")
        else:
            self.model = LogisticRegression(class_weight="balanced")

    def fit(self, csv_dataset: str):
        """
        Trains the calibrator using exported journal data.
        """
        if not os.path.exists(csv_dataset):
            print("Dataset not found!")
            return False
            
        df = pd.read_csv(csv_dataset)
        df.dropna(inplace=True)
        
        if len(df) < 50:
            print("Insufficient data for calibration! Need at least 50 samples.")
            return False
            
        features = ["entry_price", "model_edge", "latency_detected"]
        X = df[features]
        y = df["fill_success"]
        
        X_scaled = self.scaler.fit_transform(X)
        self.model.fit(X_scaled, y)
        
        # Save model
        os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
        joblib.dump({"model": self.model, "scaler": self.scaler}, self.model_path)
        print("Model calibrated and saved.")
        return True

    def predict_fill_probability(self, entry_price: float, edge: float, latency: float) -> float:
        """
        Predicts the real-world probability of a maker order getting filled 
        based on the provided parameters.
        """
        if self.model is None:
            # Fallback heuristic if no model fitted
            if latency > 500: return 0.05
            if edge > 0.05: return 0.80
            return 0.40
            
        X_new = pd.DataFrame([{
            "entry_price": entry_price,
            "model_edge": edge,
            "latency_detected": latency
        }])
        
        X_scaled = self.scaler.transform(X_new)
        proba = self.model.predict_proba(X_scaled)
        
        # Binary target: index 1 is "fill_success = 1"
        return float(proba[0][1])

# Global instance
CALIBRATOR = FillProbabilityCalibrator()
