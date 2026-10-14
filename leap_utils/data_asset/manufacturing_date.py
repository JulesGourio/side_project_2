from datetime import datetime, timedelta

import pyspark.sql.functions as f
from pyspark.sql import DataFrame


# Define t for calculating the new date
def calculate_new_date(date_reference: str, n: int, calendar: DataFrame) -> str:
    """
    Calculate the new date after adding N manufacturing days to the given date reference

    Args:
        date_reference (str): The reference date in 'YYYY-MM-DD' format.
        n (int): The number of manufacturing days to add.
        calendar (DataFrame): Customer Manufacturing Calendar.

    Returns:
        str: The new date after adding N manufacturing days in 'YYYY-MM-DD' format, or
             None when error is raised.
    """
    # Convert string to datetime
    try:
        date_reference = datetime.strptime(date_reference, "%Y-%m-%d")
    except ValueError:
        print("Your input date does not respect the date format YYYY-MM-DD")
        return None

    # Initialize a counter for manufacturing days
    manufacturing_days_count = 0

    # Iterate until we reach the desired number of manufacturing days
    while manufacturing_days_count < n:
        # Increment the date by one day
        date_reference += timedelta(days=1)

        # Check if the incremented date is a manufacturing day
        try:
            if (
                calendar.filter(f.col("Date") == date_reference)
                .select("M_Day")
                .collect()[0]["M_Day"]
                is not None
            ):  # Assuming Monday to Friday are manufacturing days
                manufacturing_days_count += 1
        except IndexError:
            print(
                "The requested date is out of calendar range, more dates need to be "
                "ingested"
            )
            return None

    # Convert the result back to string format
    new_date = date_reference.strftime("%Y-%m-%d")
    return new_date
