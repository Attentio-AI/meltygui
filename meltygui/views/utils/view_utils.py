import torch


def print_ascii_tensor(tensors, border=True, indices=None, spacing=2, names=None):
    """
    Prints an ASCII representation of one or more PyTorch tensors horizontally.
    - Zeros are replaced with '#' symbols
    - Single-digit numbers are shown as is
    - Multi-digit positive numbers are shown as '+'
    - Multi-digit negative numbers are shown as '-'

    For tensors with more than 2 dimensions, this function will use the last two dimensions
    by default, or you can specify which indices to use for higher dimensions.

    Args:
        tensors (torch.Tensor or list): A PyTorch tensor or list of tensors
        border (bool): Whether to add an ASCII border around each tensor (default: True)
        indices (tuple or None): Specific indices to use for dimensions beyond the last two.
                               For a 4D tensor, this would be a tuple of 2 indices.
        spacing (int): Number of spaces between tensors (default: 2)
        names (list or None): Optional list of names for each tensor. If provided, must match
                            the number of tensors. Names will be displayed above each tensor.

    Example:
        >>> x = torch.tensor([[1, 0, 0], [1, 1, 0], [1, 1, 1]])
        >>> y = torch.tensor([[0, 0, 2], [0, 3, 2], [4, 3, 2]])
        >>> print_ascii_tensor([x, y], names=["Identity", "Values"])
        Identity   Values
        +-----+    +-----+
        | 1 # # |  | # # 2 |
        | 1 1 # |  | # 3 2 |
        | 1 1 1 |  | 4 3 2 |
        +-----+    +-----+
    """
    # Convert single tensor to list for uniform processing
    if isinstance(tensors, torch.Tensor):
        tensors = [tensors]

    # Validate names if provided
    if names is not None:
        if len(names) != len(tensors):
            raise ValueError(f"Number of names ({len(names)}) doesn't match number of tensors ({len(tensors)})")

    # Process each tensor into a list of string rows
    all_tensor_rows = []
    max_heights = []
    tensor_widths = []

    for tensor_idx, tensor in enumerate(tensors):
        # Handle tensors with more than 2 dimensions
        tensor_dim = tensor.dim()
        if tensor_dim > 2:
            # For tensors with more than 2 dimensions, extract the 2D slice to display
            if indices is None:
                # Default: use first indices for all but the last two dimensions
                slice_indices = tuple([0] * (tensor_dim - 2))
            else:
                # Use provided indices
                if len(indices) != tensor_dim - 2:
                    raise ValueError(f"Expected {tensor_dim - 2} indices but got {len(indices)}")
                slice_indices = indices

            # Extract the 2D slice from the tensor
            tensor_slice = tensor
            for idx in slice_indices:
                tensor_slice = tensor_slice[idx]

            tensor = tensor_slice

        # Ensure the tensor is 2D at this point
        if tensor.dim() != 2:
            raise ValueError("Each tensor must have at least 2 dimensions")

        # Convert tensor to CPU and get its values as a numpy array
        tensor_np = tensor.cpu().numpy()

        # Convert tensor to string representation
        rows = []
        max_width = 0

        for row in tensor_np:
            row_str = ""
            for val in row:
                if val == 0:
                    row_str += "0 "
                else:
                    # Convert to integer if it's a whole number
                    if float(val).is_integer():
                        val = int(val)

                    # Display single-digit numbers as is, use symbols for multi-digit numbers
                    if -9 <= val <= 9:
                        row_str += f"{val} "
                    elif val > 0:
                        row_str += "+ "
                    else:  # val < 0
                        row_str += "- "

            rows.append(row_str.rstrip())  # Remove trailing space
            max_width = max(max_width, len(row_str.rstrip()))

        # Add border if needed
        tensor_rows = []
        if border:
            # Create top border
            border_line = "+" + "-" * (max_width + 2) + "+"
            tensor_rows.append(border_line)

            # Add each row with side borders
            for row in rows:
                # Calculate padding to ensure the right border aligns perfectly
                padding = max_width - len(row)
                tensor_rows.append(f"| {row}{' ' * padding} |")

            # Create bottom border
            tensor_rows.append(border_line)
        else:
            # Use rows without border
            tensor_rows = rows

        all_tensor_rows.append(tensor_rows)
        max_heights.append(len(tensor_rows))

        # Record width of this tensor's rows
        if len(tensor_rows) > 0:
            tensor_widths.append(len(tensor_rows[0]))
        else:
            tensor_widths.append(0)

    # Print tensor names if provided
    if names is not None:
        name_line = ""
        for tensor_idx, name in enumerate(names):
            # Center name over tensor width
            tensor_width = tensor_widths[tensor_idx]

            # If name is longer than tensor, truncate or allow overflow
            if len(name) > tensor_width:
                # Let's allow overflow for readability
                centered_name = name
            else:
                # Center the name
                padding = (tensor_width - len(name)) // 2
                centered_name = " " * padding + name

            name_line += centered_name

            # Add spacing between tensors (except after the last one)
            if tensor_idx < len(tensors) - 1:
                name_line += " " * spacing

        print(name_line)

    # Find the maximum height across all tensors
    max_height = max(max_heights)

    # Print all tensors horizontally
    for row_idx in range(max_height):
        row_str = ""
        for tensor_idx, tensor_rows in enumerate(all_tensor_rows):
            # If this tensor has fewer rows than the max height, print empty space
            if row_idx < len(tensor_rows):
                row_str += tensor_rows[row_idx]
            else:
                # Add empty space for the width of this tensor's representation
                if len(tensor_rows) > 0:  # Make sure tensor has at least one row
                    row_str += " " * len(tensor_rows[0])

            # Add spacing between tensors (except after the last one)
            if tensor_idx < len(all_tensor_rows) - 1:
                row_str += " " * spacing

        print(row_str)
    print("\n")


def format_time(seconds):
    """
    Convert seconds to a human-readable time string with associated color.

    Args:
        seconds (float): Time in seconds

    Returns:
        tuple: (formatted_time_string, color_tuple)
              where color_tuple is (r, g, b) values from 0-1
    """
    # Handle negative time
    if seconds is None:
        return "Unknown time", (1.0, 0.0, 0.0)

    if seconds < 0:
        time_str, color = format_time(-seconds)
        return f"-{time_str}", color

    # Very small time periods - golden yellow (1.0, 0.84, 0)
    golden_yellow = (1.0, 0.84, 0.0)
    if seconds < 1:
        return f"0 seconds", golden_yellow

    # Seconds - golden yellow (1.0, 0.84, 0)
    if seconds < 60:
        return f"{seconds:.0f} seconds", golden_yellow

    # Minutes - warm red (0.86, 0.24, 0.2)
    warm_red = (0.96, 0.44, 0.4)
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f} minutes", warm_red

    # Hours - greenish (0.29, 0.71, 0.31)
    greenish = (0.29, 0.71, 0.31)
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f} hours", greenish

    # Days - greenish (0.29, 0.71, 0.31)
    days = hours / 24
    if days < 7:
        return f"{days:.1f} days", greenish

    # Weeks - greenish (0.29, 0.71, 0.31)
    weeks = days / 7
    if weeks < 4.35:  # Approximate weeks in a month
        return f"{weeks:.1f} weeks", greenish

    # Months - greenish (0.29, 0.71, 0.31)
    months = days / 30.44  # Average days in a month
    if months < 12:
        return f"{months:.1f} months", greenish

    # Years - yellow orange (0.94, 0.59, 0.2)
    yellow_orange = (0.94, 0.59, 0.2)
    years = days / 365.25
    if years < 10:
        return f"{years:.1f} years", yellow_orange

    # Decades - dark bluish grey (0.27, 0.35, 0.43)
    dark_bluish_grey = (0.27, 0.35, 0.43)
    if years < 100:
        return f"{years:.1f} years", dark_bluish_grey

    if years < 1_000:
        return f"{years:.1f} years", dark_bluish_grey

    if years < 1_000_000:
        return f"{years / 1_000:.1f} thousand years", dark_bluish_grey

    # Millions of years - dark red (0.55, 0.12, 0.12)
    dark_red = (0.55, 0.12, 0.12)
    if years < 1_000_000_000:
        return f"{years / 1_000_000:.1f} million years", dark_red

    # Billions of years and beyond - blue (0.12, 0.31, 0.71)
    blue = (0.12, 0.31, 0.71)

    # Use descriptive strings instead of numerical values for billion+ years
    if years < 4.5e9:  # Age of Earth ~4.5 billion years
        return "Age of the Earth", blue

    if years < 5e9:  # Approximate time until Sun begins to expand significantly
        return "Time until the Sun begins expanding", blue

    if years < 7.6e9:  # Time until Sun engulfs Earth's orbit
        return "Time until the Sun engulfs Earth", blue

    if years < 1e14:  # Time until all stars burn out
        return "Era of stellar extinction", blue

    if years < 1e40:  # Deep time
        return "Approaching heat death of the universe", blue

    return "Beyond heat death of the universe", blue