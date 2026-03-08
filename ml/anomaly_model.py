import numpy as np

def detect_anomalies(amounts, threshold=2.5):
    """
    Detect anomalies using Z-score.
    Returns indices of unusual expenses.
    """

    if not amounts or len(amounts) < 5:
        return []

    data = np.array(amounts)

    mean = np.mean(data)
    std = np.std(data)

    if std == 0:
        return []

    z_scores = np.abs((data - mean) / std)

    # Return indices where value is unusual
    anomalies = np.where(z_scores > threshold)[0]

    return anomalies.tolist()