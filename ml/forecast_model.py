from sklearn.linear_model import LinearRegression
import numpy as np

def predict_next_month(expenses):
    # expenses = [12000, 15000, 13000, 16000]
    if len(expenses) < 2:
        return sum(expenses) if expenses else 0

    X = np.arange(len(expenses)).reshape(-1, 1)
    y = np.array(expenses)

    model = LinearRegression()
    model.fit(X, y)

    next_month = model.predict([[len(expenses)]])[0]
    return max(0, round(next_month))
