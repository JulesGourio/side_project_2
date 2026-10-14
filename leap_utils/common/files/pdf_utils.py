import logging
import re
import shutil
from collections import defaultdict
from datetime import datetime
from os import PathLike
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import fitz  # PyMuPDF
import pandas as pd
import pdfplumber

from ingestion.Customs_Maroc.ingestion_app import constants
from leap_utils.common import logger

LOGGER = logger.get_logger(__name__)

# avoid unwanted warnings
logging.getLogger("pdfminer").setLevel(logging.ERROR)


def get_word_position(
    page: pdfplumber.page.Page, target_word: str, position: str = "bottom"
) -> Optional[float]:
    """
    Searches for a specific word in a PDF page and returns its vertical position.

    :param page: The PDF page to analyze.
    :type page: pdfplumber.page.Page
    :param target_word: The word to search for.
    :type target_word: str
    :param position: "top" for the top position, "bottom" for the bottom position.
    :type position: str

    :return: The requested position value or None if the word is not found.
    :rtype: Optional[float]
    """
    words = page.extract_words()

    for word in words:
        if word["text"] == target_word:
            # LOGGER.debug(f"Coordinates of {target_word}: {word}")
            return word[position]  # Returns either word['top'] or word['bottom']

    return None  # Returns None if the word is not found


def add_horizontal_line(
    page: pdfplumber.page.Page, y: float = 0
) -> pdfplumber.page.Page:
    """
    Simulates the addition of a horizontal line at y=0 by modifying the list of lines extracted by pdfplumber.

    :param page: The PDF page to modify.
    :type page: pdfplumber.page.Page
    :param y: The y-coordinate of the horizontal line.
    :type y: float
    :return: The modified PDF page.
    :rtype: pdfplumber.page.Page
    """
    page.lines.append(
        {
            "x0": 0,
            "x1": page.width,
            "y0": y,
            "y1": y,
            "doctop": y,
            "top": 0,
            "bottom": 0,
            "width": page.width,
        }
    )
    return page


def find_line_coordinates(
    page: pdfplumber.page.Page, target_text: str, tolerance: int = 2
) -> Optional[Tuple[float, float]]:
    """
    Returns the coordinates of the line containing the target text.

    :param page: The PDF page to search.
    :type page: pdfplumber.page.Page
    :param target_text: The text to find within the page.
    :type target_text: str
    :param tolerance: Tolerance for grouping words by vertical position.
    :type tolerance: int
    :return: The top and bottom coordinates of the line containing the target text.
    :rtype: Optional[Tuple[float, float]]
    """
    lines_by_position = defaultdict(list)
    words = page.extract_words()
    for word in words:
        top = round(word["top"] / tolerance) * tolerance
        lines_by_position[top].append(word)

    for line_words in lines_by_position.values():
        normalized_line_text = "".join(
            [w["text"] for w in sorted(line_words, key=lambda w: w["x0"])]
        ).replace(" ", "")
        if target_text.replace(" ", "") in normalized_line_text:
            # Found the line, retrieve the coordinates of all its words
            tops = [w["top"] for w in line_words]
            bottoms = [w["bottom"] for w in line_words]
            # LOGGER.debug(f"Coordinates of {target_text}: {min(tops)},{max(bottoms)} ")
            return min(tops), max(bottoms)

    return None  # if not found


def group_words_by_line(words: List[Dict[str, Any]], tolerance: int = 2) -> List[str]:
    """
    Groups words by their vertical position into  lines of text (not PDF lines) and returns the lines as joined text.

    :param words: List of word dictionaries extracted from a PDF page.
    :type words: List[Dict[str, Any]]
    :param tolerance: Tolerance for grouping words by vertical position.
    :type tolerance: int
    :return: List of lines as joined text.
    :rtype: List[str]
    """
    lines_by_position = defaultdict(list)
    for word in words:
        top = round(word["top"] / tolerance) * tolerance
        lines_by_position[top].append(word)

    grouped_lines = []
    for line in lines_by_position.values():
        sorted_line = sorted(line, key=lambda w: w["x0"])  # sort words on the line
        text_line = " ".join([w["text"] for w in sorted_line])
        grouped_lines.append(text_line)

    return grouped_lines


def add_pdf_border(input_path: Path, output_path: Path) -> None:
    """
    Add a border to each page of a PDF file.

    :param input_path: Path to the input PDF file.
    :type input_path: Path
    :param output_path: Path to save the output PDF file with borders.
    :type output_path: Path
    :return: None
    """
    doc = fitz.open(input_path)
    for page in doc:
        width, height = page.rect.width, page.rect.height
        borders = [
            ((0, 0), (width, 0)),
            ((0, height), (width, height)),
            ((0, 0), (0, height)),
            ((width, 0), (width, height)),
        ]
        for p1, p2 in borders:
            page.draw_line(p1, p2, color=(0, 0, 0), width=1)
    doc.save(output_path)
    doc.close()


def extract_lines(pdf_path: Path) -> Tuple[Dict[int, List[Tuple]], Dict[int, List[Tuple]]]:
    """
    Extracts vertical and horizontal lines from a PDF file.

    :param pdf_path: Path to the PDF file.
    :type pdf_path: Path
    :return: A tuple containing two mappings - vertical lines and horizontal lines
        (indexed by page number).
    :rtype: Tuple[Dict[int, List[Tuple]], Dict[int, List[Tuple]]]
    """
    vertical_lines = defaultdict(list)
    horizontal_lines = defaultdict(list)
    with pdfplumber.open(pdf_path) as pdf:
        for page_number, page in enumerate(pdf.pages):
            for line in page.lines:
                x0, y0, x1, y1 = line["x0"], line["top"], line["x1"], line["bottom"]
                if x0 == x1:
                    vertical_lines[page_number].append((x0, y0, x1, y1))
                elif y0 == y1:
                    horizontal_lines[page_number].append((x0, y0, x1, y1))
    return vertical_lines, horizontal_lines


def correct_incomplete_lines_v1(
    input_path: Path,
    output_path: Path,
    vertical_lines: List[Tuple],
    horizontal_lines: List[Tuple],
    x_completion: int,
    y_top_completion: int,
    y_bottom_completion: int,
    x_len_min: int,
    y_len_min: int,
    border_width: int = 1,
    connect_disjoint_verticals: bool = False,
) -> None:
    """
    Corrects incomplete lines in a PDF document.

    :param input_path: Path to the input PDF file.
    :type input_path: Path
    :param output_path: Path to the output PDF file.
    :type output_path: Path
    :param vertical_lines: List of vertical lines in the PDF.
    :type vertical_lines: List[Tuple]
    :param horizontal_lines: List of horizontal lines in the PDF.
    :type horizontal_lines: List[Tuple]
    :param x_completion: Maximum distance to complete horizontal lines.
    :type x_completion: int
    :param y_top_completion: Maximum distance to complete vertical lines upwards.
    :type y_top_completion: int
    :param y_bottom_completion: Maximum distance to complete vertical lines downwards.
    :type y_bottom_completion: int
    :param x_len_min: Minimum length of horizontal lines to be corrected.
    :type x_len_min: int
    :param y_len_min: Minimum length of vertical lines to be corrected.
    :type y_len_min: int
    :param border_width: Width of the border lines, defaults to 1.
    :type border_width: int, optional
    :return: None
    """
    doc = fitz.open(input_path)
    for page_number, page in enumerate(doc):
        for page_number_h, x0, y0, x1, y1 in horizontal_lines:
            for page_number_v, vx0, vy0, vx1, vy1 in vertical_lines:
                # Correction of horizontal lines
                if (
                    abs(x1 - vx0) > 0
                    and abs(x1 - vx0) <= x_completion
                    and (y0 >= vy0 and y0 <= vy1)
                    and (page_number_h == page_number_v)
                    and (page_number == page_number_h)
                ):
                    LOGGER.debug(
                        f"Incomplete line detected on page {page_number + 1} at Y={y0}, correcting X1={x1} to X1={vx0}"
                    )
                    # Complete the line to the vertical line on the right
                    page.draw_line(
                        (x1, y0), (vx0, y0), color=(0, 0, 0), width=border_width
                    )

                if (
                    abs(x0 - vx0) > 0
                    and abs(x0 - vx0) <= x_completion
                    and (y0 >= vy0 and y0 <= vy1)
                    and (page_number_h == page_number_v)
                    and (page_number == page_number_h)
                ):
                    LOGGER.debug(
                        f"Incomplete line detected on page {page_number + 1} at Y={y0}, correcting X0={x0} to X0={vx0}"
                    )
                    # Complete the line to the vertical line on the left
                    page.draw_line(
                        (vx0, y0), (x0, y0), color=(0, 0, 0), width=border_width
                    )

        # Sort horizontal lines of this page by Y position
        horizontal_lines_page = sorted(
            [
                (x0, y0, x1, y1)
                for pn, x0, y0, x1, y1 in horizontal_lines
                if pn == page_number
            ],
            key=lambda line: line[1],  # Sort by x0
        )
        for page_number_v, vx0, vy0, vx1, vy1 in vertical_lines:
            if page_number_v != page_number:
                continue

            # Find the closest horizontal line above
            closest_top_y = next(
                (
                    y0 for _, y0, _, _ in reversed(horizontal_lines_page) if y0 <= vy0
                ),  # Reverse traversal (bottom to top)
                None,
            )

            # Find the closest horizontal line below
            closest_bottom_y = next(
                (y0 for _, y0, _, _ in horizontal_lines_page if y0 >= vy1),
                None,  # Normal traversal (top to bottom)
            )

            # Correction of the vertical line upwards (if a horizontal line exists)
            if (
                closest_top_y
                and abs(vy0 - closest_top_y) <= y_top_completion
                and abs(vy0 - closest_top_y) > 0
                and abs(vy1 - vy0) >= y_len_min
            ):
                LOGGER.debug(
                    f"Correcting vertical line on page {page_number+1}, X={vx0}, from Y0={vy0} to Y0={closest_top_y}"
                )
                page.draw_line(
                    (vx0, vy0),
                    (vx0, closest_top_y),
                    color=(0, 0, 0),
                    width=border_width,
                )

            # Correction of the vertical line downwards (if a horizontal line exists)
            if (
                closest_bottom_y
                and abs(vy1 - closest_bottom_y) <= y_bottom_completion
                and abs(vy1 - closest_bottom_y) > 0
                and abs(vy1 - vy0) >= y_len_min
            ):
                LOGGER.debug(
                    f"Correcting vertical line on page {page_number+1}, X={vx0}, from Y1={vy1} to Y1={closest_bottom_y}"
                )
                page.draw_line(
                    (vx0, vy1),
                    (vx0, closest_bottom_y),
                    color=(0, 0, 0),
                    width=border_width,
                )

        # Optionally connect disjoint vertical segments that are aligned on X and close in Y
        if connect_disjoint_verticals:
            vertical_lines_sorted = sorted(
                vertical_lines,
                key=lambda line: (
                    line[0],
                    line[1],
                    line[2],
                ),  # sort by page, then by x0, then by y0
            )
            for i, (page_v1, x1, y1_start, _, y1_end) in enumerate(
                vertical_lines_sorted
            ):
                if page_v1 != page_number:
                    continue
                for j, (page_v2, x2, y2_start, _, y2_end) in enumerate(
                    vertical_lines_sorted
                ):
                    if i >= j or page_v2 != page_number:
                        continue
                    if abs(x1 - x2) <= 1:  # same column
                        # check if they are close on Y
                        if (
                            y2_start > y1_end
                            and abs(y2_start - y1_end) <= y_bottom_completion
                        ):
                            LOGGER.debug(
                                f"Connecting disjoint vertical segments on page {page_number+1} en X={x1} de Y={y1_start} à Y={y2_end}"
                            )
                            page.draw_line(
                                (x1, y1_start),
                                (x1, y2_end),
                                color=(0, 0, 0),
                                width=border_width,
                            )
                        elif (
                            y1_start > y2_end
                            and abs(y1_start - y2_end) <= y_bottom_completion
                        ):
                            LOGGER.debug(
                                f"Connecting disjoint vertical segments on page {page_number+1} en X={x1} de Y={y2_start} à Y={y1_end}"
                            )
                            page.draw_line(
                                (x1, y2_start),
                                (x1, y1_end),
                                color=(0, 0, 0),
                                width=border_width,
                            )

    doc.save(output_path)
    doc.close()


def _remove_covered_vertical_lines(lines, x_tolerance=0):
    """
    Remove vertical lines that are covered by other vertical lines within a given tolerance.

    :param lines: List of lines represented as tuples (x1, y1, x2, y2).
    :type lines: list of tuple
    :param x_tolerance: Tolerance for considering lines as vertically aligned, defaults to 0.
    :type x_tolerance: int, optional
    :return: List of cleaned lines with covered lines removed.
    :rtype: list of tuple
    """
    cleaned_lines = []

    for i, (x1, y1a, x2, y2a) in enumerate(lines):
        if x1 != x2:
            continue  # not a vertical line

        y1, y2 = sorted((y1a, y2a))  # ensure y1 < y2
        is_covered = False

        for j, (x3, y3a, x4, y4a) in enumerate(lines):
            if i == j or x3 != x4:
                continue

            if abs(x1 - x3) <= x_tolerance:
                y3, y4 = sorted((y3a, y4a))

                # check if [y1, y2] is entirely contained within [y3, y4]
                if y1 >= y3 and y2 <= y4 and (y4 - y3) > (y2 - y1):
                    is_covered = True
                    break

        if not is_covered:
            cleaned_lines.append((x1, y1a, x2, y2a))  # keep original values

    return cleaned_lines


def correct_incomplete_lines(
    input_path: Path,
    output_path: Path,
    vertical_lines: Dict[int, List[Tuple]],
    horizontal_lines: Dict[int, List[Tuple]],
    x_completion: int,
    y_top_completion: int,
    y_bottom_completion: int,
    x_len_min: int,
    y_len_min: int,
    border_width: int = 1,
    connect_disjoint_verticals: bool = False,
) -> None:
    """
    Corrects incomplete horizontal and vertical lines in a PDF document.

    - Extends incomplete horizontal lines to connect with nearby vertical lines.
    - Extends vertical lines upward or downward to meet nearby horizontal lines.
    - Optionally, connects disjoint vertical segments that are aligned and close together on the Y-axis.

    :param input_path: Path to the input PDF file.
    :type input_path: Path
    :param output_path: Path to the output PDF file.
    :type output_path: Path
    :param vertical_lines: A mapping {page number -> list of vertical lines in this page}.
        Vertical lines are represented as (x0, y0, x1, y1).
    :type vertical_lines: Dict[int, List[Tuple]]
    :param horizontal_lines: A mapping {page number -> list of horizontal lines in this page}.
        Horizontal lines are represented as (x0, y0, x1, y1).
    :type horizontal_lines: Dict[int, List[Tuple]]
    :param x_completion: Maximum allowed distance to complete horizontal lines to nearby vertical lines (along X-axis).
    :type x_completion: int
    :param y_top_completion: Maximum allowed distance to extend vertical lines upward to meet a horizontal line.
    :type y_top_completion: int
    :param y_bottom_completion: Maximum allowed distance to extend vertical lines downward to meet a horizontal line.
    :type y_bottom_completion: int
    :param x_len_min: Minimum length for horizontal lines to be eligible for correction.
    :type x_len_min: int
    :param y_len_min: Minimum length for vertical lines to be eligible for correction.
    :type y_len_min: int
    :param border_width: Width of the lines drawn for corrections, defaults to 1.
    :type border_width: int, optional
    :param connect_disjoint_verticals: Whether to connect vertical segments that are aligned and close together.
    :type connect_disjoint_verticals: bool, optional
    :return: None
    """
    doc = fitz.open(input_path)
    for page_number, page in enumerate(doc):
        # Correction of horizontal lines
        for x0, y0, x1, y1 in horizontal_lines[page_number]:
            for vx0, vy0, vx1, vy1 in vertical_lines[page_number]:
                if (
                    abs(x1 - vx0) > 0
                    and abs(x1 - vx0) <= x_completion
                    and (y0 >= vy0 and y0 <= vy1)
                ):
                    LOGGER.debug(
                        f"Incomplete line detected on page {page_number + 1} at Y={y0}, correcting X1={x1} to X1={vx0}"
                    )
                    # Complete the line to the vertical line on the right
                    page.draw_line(
                        (x1, y0), (vx0, y0), color=(0, 0, 0), width=border_width
                    )

                if (
                    abs(x0 - vx0) > 0
                    and abs(x0 - vx0) <= x_completion
                    and (y0 >= vy0 and y0 <= vy1)
                ):
                    LOGGER.debug(
                        f"Incomplete line detected on page {page_number + 1} at Y={y0}, correcting X0={x0} to X0={vx0}"
                    )
                    # Complete the line to the vertical line on the left
                    page.draw_line(
                        (vx0, y0), (x0, y0), color=(0, 0, 0), width=border_width
                    )

        # Correction of vertical lines
        pending_vertical_lines = []
        horizontal_lines_sorted = sorted(
            horizontal_lines[page_number],
            key=lambda line: line[1],  # Sort horizontal lines of this page by y0
        )
        for vx0, vy0, vx1, vy1 in vertical_lines[page_number]:
            # Find the closest horizontal line above
            closest_top_y = next(
                (
                    y0 for _, y0, _, _ in reversed(horizontal_lines_sorted) if y0 <= vy0
                ),  # Reverse traversal (bottom to top)
                None,
            )

            # Find the closest horizontal line below
            closest_bottom_y = next(
                (y0 for _, y0, _, _ in horizontal_lines_sorted if y0 >= vy1),
                None,  # Normal traversal (top to bottom)
            )

            # Correction of the vertical line upwards (if a horizontal line exists)
            if (
                closest_top_y
                and abs(vy0 - closest_top_y) <= y_top_completion
                and abs(vy0 - closest_top_y) > 0
                and abs(vy1 - vy0) >= y_len_min
            ):
                LOGGER.debug(
                    f"Correcting vertical line on page {page_number+1}, X={vx0}, from Y0={vy0} to Y0={closest_top_y}"
                )
                pending_vertical_lines.append((vx0, vy0, vx0, closest_top_y))

            # Correction of the vertical line downwards (if a horizontal line exists)
            if (
                closest_bottom_y
                and abs(vy1 - closest_bottom_y) <= y_bottom_completion
                and abs(vy1 - closest_bottom_y) > 0
                and abs(vy1 - vy0) >= y_len_min
            ):
                LOGGER.debug(
                    f"Correcting vertical line on page {page_number+1}, X={vx0}, from Y1={vy1} to Y1={closest_bottom_y}"
                )
                pending_vertical_lines.append((vx0, vy1, vx0, closest_bottom_y))

        # Optionally connect disjoint vertical segments that are aligned on X and close in Y
        if connect_disjoint_verticals:
            vertical_lines_sorted = sorted(
                vertical_lines[page_number],
                key=lambda line: (
                    line[0],
                    line[1],
                ),  # sort by x0, then by y0
            )
            for i, (x1, y1_start, _, y1_end) in enumerate(
                vertical_lines_sorted
            ):
                for j, (x2, y2_start, _, y2_end) in enumerate(
                    vertical_lines_sorted
                ):
                    if i >= j:
                        continue
                    if abs(x1 - x2) == 0:  # same column
                        # check if they are close on Y
                        if (
                            y2_start > y1_end
                            and abs(y2_start - y1_end) <= y_bottom_completion
                        ):
                            LOGGER.debug(
                                f"Connecting disjoint vertical segments on page {page_number+1} en X={x1} de Y={y1_start} à Y={y2_end}"
                            )
                            pending_vertical_lines.append((x1, y1_start, x1, y2_end))
                        elif (
                            y1_start > y2_end
                            and abs(y1_start - y2_end) <= y_bottom_completion
                        ):
                            LOGGER.debug(
                                f"Connecting disjoint vertical segments on page {page_number+1} en X={x1} de Y={y2_start} à Y={y1_end}"
                            )
                            pending_vertical_lines.append((x1, y2_start, x1, y1_end))

        # deduplicate and draw new vertical lines
        unique_vertical_lines = set()
        for x0, y0, x1, y1 in pending_vertical_lines:
            p1, p2 = sorted([(x0, y0), (x1, y1)])
            unique_vertical_lines.add((p1[0], p1[1], p2[0], p2[1]))
        cleaned_unique_vertical_lines = _remove_covered_vertical_lines(unique_vertical_lines)
        for x0, y0, x1, y1 in cleaned_unique_vertical_lines:
            page.draw_line((x0, y0), (x1, y1), color=(0, 0, 0), width=border_width)

    doc.save(output_path)
    doc.close()


def process_pdf_lines_completion(
    input_pdf_path: PathLike | str,
    output_pdf_path: PathLike | str,
    temporary_path: PathLike | str,
    add_border: bool = True,
    x_completion: int = 5,
    y_top_completion: int = 5,
    y_bottom_completion: int = 5,
    x_len_min: int = 5,
    y_len_min: int = 5,
    cleanup: bool = True,
    connect_disjoint_verticals: bool = False,
) -> None:
    """
    Processes a PDF by adding borders and completing incomplete lines.

    :param input_pdf_path: Path to the input PDF file.
    :type input_pdf_path: PathLike | str
    :param output_pdf_path: Path to where the output PDF file will be saved.
    :type output_pdf_path: PathLike | str
    :param temporary_path: Path to a temporary folder, where intermediate files will
        be stored. The cleanup of these files is controlled via the argument ``cleanup``
    :type temporary_path: PathLike | str
    :param add_border: Whether to add a border to the PDF before processing, defaults to True.
    :type add_border: bool, optional
    :param x_completion: Maximum distance to complete horizontal lines, defaults to 5.
    :type x_completion: int, optional
    :param y_top_completion: Maximum distance to complete vertical lines upwards, defaults to 5.
    :type y_top_completion: int, optional
    :param y_bottom_completion: Maximum distance to complete vertical lines downwards, defaults to 5.
    :type y_bottom_completion: int, optional
    :param x_len_min: Minimum length of horizontal lines to be corrected, defaults to 5.
    :type x_len_min: int, optional
    :param y_len_min: Minimum length of vertical lines to be corrected, defaults to 5.
    :type y_len_min: int, optional
    :param cleanup: Whether to delete temporary files after processing, defaults to True.
    :type cleanup: bool, optional
    :param connect_disjoint_verticals: If True, connects vertical lines that are aligned
        and close but disjoint, defaults to False.
    :type connect_disjoint_verticals: bool, optional
    :rtype: None
    """

    input_path = Path(input_pdf_path)
    temp_path = Path(f"{temporary_path}/temp_with_border.pdf")
    corrected_path = Path(f"{temporary_path}/temp_corrected.pdf")

    # Step 1: Add border to the PDF (Only if add_border is True)
    pdf_to_process = input_path
    if add_border:
        add_pdf_border(input_path, temp_path)
        pdf_to_process = temp_path

    # Step 2: Open the PDF to correct incomplete lines
    # Extract existing lines from the PDF
    vertical_lines, horizontal_lines = extract_lines(pdf_to_process)

    # Step 3: Correct incomplete lines
    correct_incomplete_lines(
        input_path=pdf_to_process,
        output_path=corrected_path,
        vertical_lines=vertical_lines,
        horizontal_lines=horizontal_lines,
        x_completion=x_completion,
        y_top_completion=y_top_completion,
        y_bottom_completion=y_bottom_completion,
        x_len_min=x_len_min,
        y_len_min=y_len_min,
        connect_disjoint_verticals=connect_disjoint_verticals,
    )

    # Step 4: Copy the corrected file to the final output
    shutil.copy(corrected_path, output_pdf_path)
    LOGGER.info(f"Corrected PDF saved as {output_pdf_path}")

    if cleanup:
        temp_path.unlink(missing_ok=True)
        corrected_path.unlink(missing_ok=True)


def _is_valid_integer(value: str) -> bool:
    """Check if a value is non-null, non-empty, and can be interpreted as an integer."""
    try:
        return value is not None and value.strip() != "" and int(value.replace(",", "").replace(" ", "")) is not None
    except (ValueError, AttributeError):
        return False


def _get_sommier_header_value(header_text: str, keyword: str) -> str | None:
    """
    Extracts the reference from the header text.

    :param header_text: The header text containing the keyword.
    :type header_text: str
    :param keyword: The keyword to search for in the header text.
    :type keyword: str
    :return: The extracted value corresponding to the keyword or None if not found.
    :rtype: str
    """
    pattern = rf"{keyword}\s*:\s*(.+)"
    match = re.search(pattern, header_text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _clip_or_pad_list(
    input_list: list,
    desired_number_of_elements: int,
    padding_value: Any,
) -> list:
    """
    Return a copy of ``input_list`` with exactly ``desired_number_of_elements``
    elements.

    If the input list has more elements, only the first are kept. If it does not have
    enough, the value ``padding_value`` will be added at the end.

    :param input_list: The input list to clip or pad
    :type input_list: list
    :param desired_number_of_elements: The desired number of elements for the output list
    :type desired_number_of_elements: int
    :param padding_value: The padding value to append at the end of the output to match
        the desired length
    :type padding_value: Any

    :return: A copy of ``input_list``, either clipped or padded to match the desired
        length.
    :rtype: list
    """
    return input_list[:desired_number_of_elements] + [padding_value] * (desired_number_of_elements - len(input_list))


def extract_sommier_header_and_main_table(
    pdf_path: PathLike | str,
    expected_number_of_columns: int,
) -> list[list]:
    """
    Extracts the header and main table from a PDF file.

    :param pdf_path: Path to the PDF file.
    :type pdf_path: PathLike | str
    :param expected_number_of_columns: The expected number of columns in the output
        table.
    :type expected_number_of_columns: int

    :return: The extracted pdf data, as a list of lists, where each "inner" list
        corresponds to a row.
    :rtype: list
    """
    table_data = []  # Stores the main table

    header_delimiter = "NOMENCLATURE"
    table_delimiter = "OMISSION"
    table_delimiter_last_page = "SOLDE GENERAL"

    pdf_name = Path(pdf_path).name

    nb_min_cols = 10
    numeric_cols_indices = [0, 1]
    float_cols_indices = [5,9] # [3, 7]

    with pdfplumber.open(pdf_path) as pdf:
        # Get the number of pages
        num_pages = len(pdf.pages)
        LOGGER.debug(f"pdf_name: {pdf_name} , num_pages : {num_pages}")

        for page_number, page in enumerate(pdf.pages):
            is_last_page = page_number == num_pages - 1
            is_first_page = page_number == 0

            # Get the width of the page
            page_width = page.width

            if is_first_page:
                # Search for the word header_delimiter and get its bottom coordinates
                table_top_position = get_word_position(page, header_delimiter, "bottom")
                height = get_word_position(page, header_delimiter, "height")
                if not table_top_position:
                    table_top_position = 400  # default value
                if not height:
                    height = 10  # default value
                LOGGER.debug(f"pdf_name: {pdf_name}, first page, table_top_position : {table_top_position}")

                # Crop the page to keep only the header
                left, top, right, bottom = 0, 0, page_width, table_top_position - height
                cropped_header = page.within_bbox((left, top, right, bottom))
                header_text = cropped_header.extract_text()
                if header_text:
                    reference = _get_sommier_header_value(header_text, "REFERENCE")
                    date_ref = _get_sommier_header_value(header_text, "DATE")
                    etat = _get_sommier_header_value(header_text, "ETAT")
                    date_echeance = _get_sommier_header_value(header_text, "DATE D'ECHEANCE")
                    demandeur = _get_sommier_header_value(header_text, "DEMANDEUR")
                    registre = _get_sommier_header_value(header_text, "REGISTRE")
                    valeur = _get_sommier_header_value(header_text, "VALEUR")
            else:
                table_top_position = 0
                # page = add_horizontal_line(page, y=0)

            if is_last_page:
                # Search for the word table_delimiter_last_page and get its top coordinates
                top, bottom = find_line_coordinates(page, table_delimiter_last_page)
                table_bottom_position = top
                if not table_bottom_position:
                    table_bottom_position = 600  # default value
                LOGGER.debug(f"pdf_name: {pdf_name}, last page, bottom_position : {table_bottom_position}")
            else:
                # Search for the word table_delimiter and get its top coordinates
                table_bottom_position = get_word_position(
                    page, table_delimiter, "top"
                ) - get_word_position(page, table_delimiter, "height")
                if not table_bottom_position:
                    table_bottom_position = 600  # default value
                LOGGER.debug(f"pdf_name: {pdf_name}, bottom_position : {table_bottom_position}")

            # Define the extraction area coordinates
            # Define the coordinates with X ranging from 0 to the width of the page and Y from table_top_position to table_bottom_position
            left, top, right, bottom = (
                0,
                table_top_position,
                page_width,
                table_bottom_position,
            )

            # Crop the page to keep only the table area
            cropped_page = page.within_bbox((left, top, right, bottom))
            LOGGER.debug(f"pdf_name: {pdf_name}, cropped_page : {left}, {top}, {right}, {bottom}")
            explicit_horizontal_lines = []

            # Extract words coordinates for words of NOMENCLATURE
            words = cropped_page.extract_words()
            nomenclature_pattern = re.compile(r"^\d{10,}$")
            nomenclature_words = [
                word for word in words if nomenclature_pattern.match(word["text"])
            ]
            for word in nomenclature_words:
                y_top_nomenclature = word["top"]
                if y_top_nomenclature > 0 :
                    y_top_nomenclature = y_top_nomenclature -1
                y_bottom_nomenclature = word["bottom"] + 1
                explicit_horizontal_lines.append(y_top_nomenclature)
                explicit_horizontal_lines.append(y_bottom_nomenclature)
                # cropped_page = add_horizontal_line(cropped_page, y=y_bottom_nomenclature)

            LOGGER.debug(f"pdf_name: {pdf_name}, raw nomenclature words : \n{nomenclature_words}")
            LOGGER.debug(f"pdf_name: {pdf_name}, explicit_horizontal_lines  : \n{explicit_horizontal_lines}")

            if len(explicit_horizontal_lines) == 0:
                continue

            # Extract tables from this area
            tables = cropped_page.extract_tables(
                {
                    "vertical_strategy": "lines",  # Use vertical lines to detect columns
                    "horizontal_strategy": "explicit",  # Use explicit horizontal lines to detect rows
                    "explicit_horizontal_lines": explicit_horizontal_lines,
                    "snap_tolerance": 5,
                    "intersection_tolerance": 6,
                }
            )
            LOGGER.debug(f"pdf_name: {pdf_name}, raw tables : \n{tables}")

            # Display only rows with at least nb_min_cols columns
            for i, table in enumerate(tables):
                filtered_rows = [
                    row
                    for row in table
                    if len(row) >= nb_min_cols
                    and all(row[i] is not None and row[i].isdigit() for i in numeric_cols_indices)
                    and all(row[i] is not None and _is_valid_integer(row[i]) for i in float_cols_indices)
                ]  # Filter only rows with nb_min_cols columns

                if filtered_rows:
                    LOGGER.debug(f"--- Page {page_number + 1}, Table {i + 1} ---")
                    LOGGER.debug(
                        f"Table with {len(filtered_rows)} rows with minimum {nb_min_cols} columns"
                    )
                    for row in filtered_rows:
                        LOGGER.debug(
                            f"({len(row)} columns) {row}"
                        )  # Display the size of each row
                        row_data = [
                            pdf_name,
                            page_number + 1,
                            reference,
                            date_ref,
                            etat,
                            date_echeance,
                            demandeur,
                            registre,
                            valeur,
                        ] + row
                        table_data.append(row_data)
                    LOGGER.debug("\n" + "=" * 50 + "\n")
                else:
                    LOGGER.debug(
                        f"No rows with nb_min_cols columns found on page {page_number + 1}, table {i + 1}, {len(table[0])} columns\n===\n{table}\n==="
                    )

    if len(table_data) == 0:
        table_data = None

    return [
        _clip_or_pad_list(
            input_list=row,
            desired_number_of_elements=expected_number_of_columns,
            padding_value=None
        ) for row in table_data
    ]


def _extract_qty_unit(text: str) -> Tuple[Optional[float], Optional[str]]:
    """
    Extracts the most probable quantity and unit from the text.
    Priority is given to quantities with a decimal separator.

    :param text: The text from which to extract the quantity and unit.
    :type text: str
    :return: A tuple containing the quantity and unit.
    :rtype: Tuple[Optional[float], Optional[str]]
    """
    # Extract all pairs (quantity, unit)
    matches = re.findall(r"(\d+(?:[.,]\d+)?)\s+([A-Z\. ]+)", text.upper())

    best_match = None
    for qty_str, unit in matches:
        unit = unit.strip()
        if len(unit) > 10:
            continue  # Unit too long, probably noise

        try:
            qty = float(qty_str.replace(",", "."))
        except ValueError:
            continue

        if "." in qty_str or "," in qty_str:
            return qty, unit  # priority to this match with a point

        if not best_match:
            best_match = (qty, unit)  # keep this match as a backup

    return best_match if best_match else (None, None)


def extract_mlv_main_table(
    pdf_path: PathLike | str,
    main_balise_aliases: dict[str, str],
    item_balise_aliases: dict[str, str],
) -> list:
    """
    Extracts the header and main table from a PDF file.

    :param pdf_path: Path to the PDF file.
    :type pdf_path: PathLike | str
    :param main_balise_aliases: Dictionary mapping main balises to their aliases.
    :type main_balise_aliases: dict
    :param item_balise_aliases: Dictionary mapping item balises to their aliases.
    :type item_balise_aliases: dict

    :return: The extracted table data, as a list of dict, where each dict represents a
        row, and has the structure {col_name: value}.
    :rtype: list
    """
    pdf_name = Path(pdf_path).name

    # Main tags (only once per PDF)
    main_balises = list(main_balise_aliases.keys())
    # Tags for each occurrence (all except empty values)
    sub_balises = list(item_balise_aliases.keys())

    table_data = []  # Stores the main table

    LOGGER.debug(f"pdf_name : {pdf_name}")
    main_balises_trouvees = set()

    with pdfplumber.open(pdf_path) as pdf:
        main_data = {"pdf_name": pdf_name}
        current_item = {}  # Store data for a single item
        current_item_num = None
        new_item_number = False

        for page_number, page in enumerate(pdf.pages):
            tables = page.extract_tables(
                {
                    "vertical_strategy": "lines",  # Use vertical lines to detect columns
                    "horizontal_strategy": "lines",  # Use horizontal lines to detect rows
                    "snap_tolerance": 10,
                    "intersection_tolerance": 14,
                }
            )

            # Extract main_balises only once
            if not any(k in main_data for k in main_balises):
                for table in tables:
                    for row in table:
                        for cell in row:
                            if cell and isinstance(cell, str):
                                for balise in main_balises:
                                    if balise and cell.startswith(balise):
                                        valeur = cell[len(balise) :].strip()
                                        alias = main_balise_aliases.get(balise, balise)
                                        main_data[alias] = valeur

            # Extract sub_balises
            for table in tables:
                for row in table:
                    has_sub_balise = False

                    for cell in row:
                        if cell and isinstance(cell, str):
                            # Special case: item_number is followed by a "designation" without a tag, so we construct the "designation" tag
                            if new_item_number:
                                LOGGER.debug(f"-> cell following item_number : {cell}")
                                balise = "designation"
                                sub_balises.append(balise)
                                cell = balise + "\n" + cell
                                item_balise_aliases[balise] = balise
                                qty, unit = _extract_qty_unit(cell)
                                LOGGER.debug(f"qty : {qty}, unit : {unit}")
                                new_item_number = False
                                if current_item:
                                    current_item["qty"] = qty
                                    current_item["unit"] = unit

                            for balise in sub_balises:
                                if balise and cell.startswith(balise):
                                    valeur = cell[len(balise) :].strip()
                                    alias = item_balise_aliases.get(balise, balise)

                                    if alias == "item_number" and valeur:
                                        new_item_number = True
                                        # If a new item_number is detected, store the old item
                                        if (
                                            current_item
                                            and current_item_num is not None
                                        ):
                                            table_data.append(current_item)

                                        # Start a new item
                                        current_item = {
                                            "pdf_name": pdf_name,
                                            "page_number": page_number + 1,
                                            **main_data,
                                        }
                                        current_item_num = valeur
                                        current_item[alias] = valeur
                                        LOGGER.debug(
                                            f"page_number: {page_number+1}, current_item_num: {current_item_num}"
                                        )
                                    elif valeur:
                                        current_item[alias] = valeur
                                        has_sub_balise = True

                    # If a sub_balise was found but no new item_number, do not create a new row
                    if has_sub_balise and current_item:
                        continue

        # Add the last item after the loop ends
        if current_item:
            table_data.append(current_item)

    if len(table_data) == 0:
        table_data = None

    return table_data


def _put_columns_first(
    df: pd.DataFrame, columns_to_put_first: list[str]
) -> pd.DataFrame:
    """
    Return a copy of DataFrame, with ``columns_to_put_first`` first.

    :param df: A pandas DataFrame
    :type df: pd.DataFrame
    :param columns_to_put_first: The list of columns to put first in the DataFrame
    :type: list[str]

    :return: A copy of the original DataFrame with reordered columns
    :rtype: pd.DataFrame
    """
    other_columns = [
        col_name for col_name in df.columns if col_name not in columns_to_put_first
    ]
    return df[[*columns_to_put_first, *other_columns]]


def _keep_first_line_of_textual_columns(
    df: pd.DataFrame, columns: list[str]
) -> pd.DataFrame:
    """
    Keep the first line of the text in each cell of the DataFrame for the specified
    textual columns.

    :param df: A pandas DataFrame
    :type df: pd.DataFrame
    :param columns: The list of textual columns for which only the 1st line of each cell
        will be kept. If some columns are not present in ``df``, they will simply be
        ignored
    :type columns: list[str]

    :return: A copy of ``df`` with only the wanted data in the specified columns.
    :rtype: pd.DataFrame
    """
    output = df.copy()
    for column in set(columns).intersection(output.columns):
        output[column] = output[column].str.split("\n").str.get(0)
    return output


def _convert_str_columns_to_double(
    df: pd.DataFrame, columns_to_convert: list[str]
) -> pd.DataFrame:
    """
    Converts textual columns in ``columns_to_convert`` of ``df`` to double.

    :param df: The initial DataFrame
    :type df: pd.DataFrame
    :param columns_to_convert: The list of columns to convert in ``df``. If some columns
        are not present in ``df``, they will simply be ignored
    :type columns_to_convert: list[str]

    :return: A copy of ``df`` with converted columns
    :rtype: pd.DataFrame
    """
    output = df.copy()
    for column in set(columns_to_convert).intersection(output.columns):
        output[column] = (
            output[column]
            .astype(str)
            .str.replace(r"\s+", "", regex=True)
            .str.replace(",", ".")
            .astype(float)
        )
    return output


def _drop_duplicates_by_order(
    df: pd.DataFrame,
    columns: list | str,
    order_col: list | str,
    keep: Literal["first", "last"] = "first",
) -> pd.DataFrame:
    """
    Drop first/last occurence of columns while respecting a certain order of order_col

    :param df: The dataframe in which columns should dropped
    :type df: pd.DataFrame
    :param columns: The columns to consider for identifying duplicates
    :type columns: list | str
    :param order_col: The column to order before removing duplicates
    :type order_col: list | str
    :param keep: {‘first’, ‘last’}, default ‘first’ Determines which duplicates (if any)
                 to keep after ordering
                 ‘first’ : Drop duplicates except for the first occurrence.
                 ‘last’ : Drop duplicates except for the last occurrence.
    :type keep: Literal["first", "last"]

    :return: The input dataframe with duplicates removed.
    :rtype: pd.DataFrame
    """
    return df.sort_values(by=order_col).drop_duplicates(
        subset=columns, keep=keep, inplace=False, ignore_index=True
    )


def _remove_newlines_from_textual_columns(
    df: pd.DataFrame, columns: list[str]
) -> pd.DataFrame:
    """
    Remove newline characters from each cell of the specified textual columns.

    :param df: A pandas DataFrame
    :type df: pd.DataFrame
    :param columns: The list of textual columns for which newline characters
                    will be removed.
                    If some columns are not present in ``df``, they will
                    simply be ignored.
    :type columns: list[str]

    :return: A copy of ``df`` with newlines removed in the specified columns.
    :rtype: pd.DataFrame
    """
    output = df.copy()
    for column in set(columns).intersection(output.columns):
        output[column] = output[column].astype(str).str.replace("\n", " ", regex=False)
    return output


def mlv_post_processing(
    df: pd.DataFrame,
    mlv_type: Literal["MLV (simplified)", "MLV (detailed)"] = "MLV (simplified)",
) -> pd.DataFrame:
    """
    Process the raw parsed MLV data :

    - strip str columns of newlines, whitespaces, etc.
    - extract the registration number, registration date, "sous-dum" and "initial dum"
    - extract the country code and country of origin
    - extract the "extraction" timestamp
    - convert numerical columns to double
    - reorder columns
    - break down designation into several fields

    :param df: A DataFrame with the parsed contents from MLV files.
    :type df: pd.DataFrame
    :param mlv_type: indicates if new or old mlv format document.
        Defaults to the old.
    :type mlv_type: str

    :return: A copy of the original DataFrame with processed columns
    :rtype: pd.DataFrame
    """

    # Remove newlines in specific columns
    df = _keep_first_line_of_textual_columns(
        df=df,
        columns=["bureau"],
    )

    # Keep all lines of designation but replace line breaks by spaces
    if "designation" in df.columns:
        if mlv_type == "MLV (detailled)":
            if all(col in df.columns for col in ["designation", "qty", "unit"]):

                def clean_designation(row: pd.Series) -> str:
                    """
                    Normalize and clean designation field:
                    - remove line breaks
                    - normalize spaces
                    - if qty/unit is present :
                    - cut after qty/unit
                    - remove qty/unit
                    """

                    text = row.get("designation", "")

                    if pd.isna(text):
                        return ""

                    text = str(text)

                    # 1. normalize line breaks
                    text = text.replace("\n", " ").strip()

                    # 2. handle qty/unit if present
                    if ("qty" in row
                        and "unit" in row
                        and pd.notna(row["qty"])
                        and pd.notna(row["unit"])
                        ):
                        qty = float(row["qty"])
                        unit = str(row["unit"])

                        patterns = [
                            f"{qty:.2f} {unit}",
                            f"{int(qty)} {unit}",
                        ]

                        for pattern in patterns:
                            pos = text.find(pattern)
                            if pos != -1:
                                # cut after qty/unit
                                text = text[:pos + len(pattern)]
                                break

                        # remove qty/unit inside text
                        text = text.replace(f"{qty:.2f} {unit}", "")
                        text = text.replace(f"{int(qty)} {unit}", "")

                    text = text.replace("N/A", "NA")

                    return text.strip()

                df["designation"] = df.apply(clean_designation, axis=1)

            def split_designation(df: pd.DataFrame) -> pd.DataFrame:
                """
                Split the designation field into several columns.

                Processing steps:
                - split designation on "/"
                - keep only rows producing exactly 5 fields
                - strip whitespaces from extracted fields
                - replace the original designation column by the extracted fields
                """
                df = df.copy()

                split_raw = df["designation"].str.split(r"\s*/\s*")

                split_raw = split_raw.apply(
                    lambda x: x if len(x) == 5 else [pd.NA] * 5)

                split_df = pd.DataFrame(
                    split_raw.tolist(),
                    columns=constants.SPLIT_DESIGNATION_COLS)

                split_df = split_df.apply(
                    lambda col:
                    col.map(lambda x: x.strip() if isinstance(x, str) else pd.NA))

                df[constants.SPLIT_DESIGNATION_COLS] = split_df
                df = df.drop(columns=["designation"])

                return df

            df = split_designation(df)

    # Check if the 'enregistrement' column exists
    if "enregistrement" in df.columns:
        # Extract the first line of 'enregistrement' (before any \n)
        df["reginfo"] = df["enregistrement"].str.split("\n").str.get(0)

        # Extract only the registration number with a regex
        df["regnum"] = df["reginfo"].str.extract(r"(\d+)", expand=False)

        # Extract the date in dd/mm/yyyy format
        df["reg_date"] = df["reginfo"].str.extract(r"(\d{2}/\d{2}/\d{4})", expand=False)

        # Extract only the year (yyyy)
        df["reg_year"] = (
            df["reg_date"]
            .str.split("/")
            .str.get(2)
            .where(
                cond=~df["reg_date"].isnull(),
                other=None,
            )
        )

        # Calculate sous_dum as : bureau + declaration + reg_year + regnum
        df["sous_dum"] = (
            df["bureau"] + df["declaration"] + df["reg_year"] + df["regnum"]
        )

        # Extract the value after "Initiale:" if it exists
        df["initial_dum"] = df["enregistrement"].str.extract(
            r"Initiale:(\S+)",
            expand=False,
        )

    # Extract the country and country code from 'country_of_origin' if the column exists
    if "country_of_origin" in df.columns:
        df["country"] = df["country_of_origin"].str.split(" ").str.get(0)
        df["country_code"] = df["country_of_origin"].str.split(" ").str.get(1)

    # Extract the timestamp in the 'depot' field
    if "depot" in df.columns:
        df["_meta_extraction_timestamp"] = pd.to_datetime(
            df["depot"].str.extract(r"(\d{2}/\d{2}/\d{4} \d{2}:\d{2})", expand=False),
            format="%d/%m/%Y %H:%M",
        )
    df["_meta_ingestion_timestamp"] = datetime.now()
    df["_meta_file_name"] = df["pdf_name"]

    # Clean and convert columns to double
    df = _convert_str_columns_to_double(
        df=df,
        columns_to_convert=[
            "value_declared",
            "total_value_declared",
            "currency_change",
            "qty",
        ],
    )

    df = df.replace(["N/A", "NA", "n/a", ""], None)

    # columns reordering
    return _put_columns_first(
        df=df,
        columns_to_put_first=[
            "_meta_extraction_timestamp",
            "_meta_ingestion_timestamp",
            "_meta_file_name",
        ],
    )


def sommier_post_processing(df: pd.DataFrame) -> pd.DataFrame:
    """
    Process the raw parsed sommier data :

    - strip str columns of newlines, whitespaces, etc.
    - extract the "extraction" timestamp
    - convert numerical columns to double
    - deduplicates rows
    - build the column "dec_apurement"
    - reorder columns

    :param df: A DataFrame with the parsed contents from sommier files. It must contain
        the columns "pdf_name", "page_number", "nomenclature", "item_number",
        "dec_apurement", "dec_apurement_2", "dec_apurement_3".
    :type df: pd.DataFrame

    :return: A copy of the original DataFrame with processed columns
    :rtype: pd.DataFrame

    """
    # Remove line breaks in specific columns
    df = _keep_first_line_of_textual_columns(
        df=df,
        columns=["gross_value"],
    )

    # Extract timestamp in the 'date' field
    if "reference_date" in df.columns:
        df["_meta_extraction_timestamp"] = pd.to_datetime(
            df["reference_date"], format="%d/%m/%Y"
        )
    df["_meta_ingestion_timestamp"] = datetime.now()
    df["_meta_file_name"] = df["pdf_name"]

    # Clean and convert columns to double
    df = _convert_str_columns_to_double(
        df=df,
        columns_to_convert=[
            "gross_value",
            "total_value_declared",
            "quantity",
        ],
    )

    # Drop duplicates
    df_dedup = _drop_duplicates_by_order(
        df=df,
        columns=["pdf_name", "nomenclature", "item_number"],
        order_col="page_number",
        keep="first",
    )

    df_dedup["dec_apurement"] = (
        df_dedup["dec_apurement"]
        + df_dedup["dec_apurement_2"]
        + df_dedup["dec_apurement_3"]
    )
    df_dedup = df_dedup.drop(columns=["dec_apurement_2", "dec_apurement_3"])

    # columns reordering
    return _put_columns_first(
        df=df_dedup,
        columns_to_put_first=[
            "_meta_extraction_timestamp",
            "_meta_ingestion_timestamp",
            "_meta_file_name",
        ],
    )


def _customs_pdf_quality_check_df(
    df: pd.DataFrame,
    column_to_sum: str,
    column_to_compare_to: str,
) -> pd.DataFrame:
    """
    Return a pandas DataFrame with 2 columns : "pdf_name" and "is_valid"

    :param df: The post-processed DataFrame extracted from an MLV or sommier pdf.
        It must contain the columns "pdf_name", ``column_to_sum``,
        ``column_to_compare_to``.
    :type df: pyspark.sql.DataFrame
    :param column_to_sum: The name of the column to sum
    :type column_to_sum: str
    :param column_to_compare_to: The name of the column the previously computed sum
        will be compared to.
    :type column_to_compare_to: str

    :return: A pandas DataFrame
    :rtype: pd.DataFrame
    """
    df_check = (
        df.groupby(by="pdf_name")
        .agg(
            {column_to_sum: "sum", column_to_compare_to: "first"},
        )
        .rename(columns={column_to_sum: f"_sum_{column_to_sum}"})
        .reset_index(drop=False)
    )

    # Add validation column
    df_check["is_valid"] = df_check[f"_sum_{column_to_sum}"].round(2) == df_check[
        column_to_compare_to
    ].round(2)

    return df_check


def customs_pdf_extracted_data_is_valid(
    df: pd.DataFrame,
    column_to_sum: str,
    column_to_compare_to: str,
) -> bool:
    """
    Check if the parsed data is valid. This check is based on the sum of individual
    goods values and the total goods value.

    :param df: The post-processed DataFrame extracted from an MLV or sommier pdf.
        It must contain the columns "pdf_name", ``column_to_sum``,
        ``column_to_compare_to``.
    :type df: pyspark.sql.DataFrame
    :param column_to_sum: The name of the column to sum
    :type column_to_sum: str
    :param column_to_compare_to: The name of the column the previously computed sum
        will be compared to.
    :type column_to_compare_to: str

    :return: Whether the total sum of goods is equal to the sum of each good's value.
    :rtype: bool
    """
    df_check = _customs_pdf_quality_check_df(
        df=df,
        column_to_sum=column_to_sum,
        column_to_compare_to=column_to_compare_to,
    )

    return df_check["is_valid"][0]


def quality_check_customs_pdf_extracted_data(
    df: pd.DataFrame,
    column_to_sum: str,
    column_to_compare_to: str,
) -> pd.DataFrame:
    """
    Add a quality check column to the dataframe.

    This check is based on the sum of individual goods values and the parsed total
    goods value.

    :param df: The post-processed DataFrame extracted from an MLV or sommier pdf.
        It must contain the columns "pdf_name", ``column_to_sum``,
        ``column_to_compare_to``.
    :type df: df.DataFrame
    :param column_to_sum: The name of the column to sum (for each file name)
    :type column_to_sum: str
    :param column_to_compare_to: The name of the column the previously computed sum
        will be compared to.
    :type column_to_compare_to: str

    :return: A copy of the original DataFrame with a boolean "is_valid" column
    :rtype: df.DataFrame
    """
    df_check = _customs_pdf_quality_check_df(
        df=df,
        column_to_sum=column_to_sum,
        column_to_compare_to=column_to_compare_to,
    )

    return df.join(
        other=df_check[["pdf_name", "is_valid"]]
        .drop_duplicates()
        .set_index("pdf_name"),
        on="pdf_name",
        how="left",
    ).fillna(value={"is_valid": False})


def extract_mhirj_supplier_list_header_and_main_table(
    pdf_path: PathLike | str,
    expected_number_of_columns: int,
) -> list[list]:
    """
    Extracts the header and main table from a PDF file.

    :param pdf_path: Path to the PDF file.
    :type pdf_path: PathLike | str
    :param expected_number_of_columns: The expected number of columns in the output
        table.
    :type expected_number_of_columns: int

    :return: The extracted pdf data, as a list of lists, where each "inner" list
        corresponds to a row.
    :rtype: list
    """
    table_data = []  # Stores the main table

    header_delimiter = "Vendor Code"
    table_delimiter = "Confidential"
    table_delimiter_last_page = "Confidential"

    pdf_name = Path(pdf_path).name

    nb_min_cols = 8
    numeric_cols_indices = [0, 1]

    with pdfplumber.open(pdf_path) as pdf:
        # Get the number of pages
        num_pages = len(pdf.pages)
        LOGGER.debug(f"pdf_name: {pdf_name} , num_pages : {num_pages}")

        for page_number, page in enumerate(pdf.pages):
            is_last_page = page_number == num_pages - 1
            is_first_page = page_number == 0

            # Get the width of the page
            page_width = page.width

            if is_first_page:
                # Search for the word header_delimiter and get its bottom coordinates
                table_top_position = get_word_position(page, header_delimiter, "bottom")
                height = get_word_position(page, header_delimiter, "height")
                if not table_top_position:
                    table_top_position = 400  # default value
                if not height:
                    height = 10  # default value
                LOGGER.debug(f"pdf_name: {pdf_name}, first page, table_top_position : {table_top_position}")

                # Crop the page to keep only the header
                left, top, right, bottom = 0, 0, page_width, table_top_position - height
                cropped_header = page.within_bbox((left, top, right, bottom))
                header_text = cropped_header.extract_text()
                if header_text:
                    reference_date = _get_sommier_header_value(header_text, "Last update")
                
                table_top_position = 0
            else:
                table_top_position = 0
                # page = add_horizontal_line(page, y=0)

            if is_last_page:
                # Search for the word table_delimiter_last_page and get its top coordinates
                top, bottom = find_line_coordinates(page, table_delimiter_last_page)
                table_bottom_position = top
                if not table_bottom_position:
                    table_bottom_position = 600  # default value
                LOGGER.debug(f"pdf_name: {pdf_name}, last page, bottom_position : {table_bottom_position}")
            else:
                # Search for the word table_delimiter and get its top coordinates
                table_bottom_position = get_word_position(
                    page, table_delimiter, "top"
                ) - get_word_position(page, table_delimiter, "height")
                if not table_bottom_position:
                    table_bottom_position = 600  # default value
                LOGGER.debug(f"pdf_name: {pdf_name}, bottom_position : {table_bottom_position}")

            # Define the extraction area coordinates
            # Define the coordinates with X ranging from 0 to the width of the page and Y from table_top_position to table_bottom_position
            left, top, right, bottom = (
                0,
                table_top_position,
                page_width,
                table_bottom_position,
            )

            # Crop the page to keep only the table area
            cropped_page = page.within_bbox((left, top, right, bottom))
            LOGGER.debug(f"pdf_name: {pdf_name}, cropped_page : {left}, {top}, {right}, {bottom}")

            # Extract tables from this area
            tables = cropped_page.extract_tables(
                {
                    "vertical_strategy": "lines",  # Use vertical lines to detect columns
                    "horizontal_strategy": "lines",  # Use horizontal lines to detect rows
                    "snap_tolerance": 5,
                    "intersection_tolerance": 6,
                }
            )
            LOGGER.debug(f"pdf_name: {pdf_name}, raw tables : \n{tables}")

            # Display only rows with at least nb_min_cols columns
            for i, table in enumerate(tables):
                filtered_rows = [
                    row
                    for row in table
                    if len(row) >= nb_min_cols
                    and all(row[i] is not None and row[i].isdigit() for i in numeric_cols_indices)
                ]  # Filter only rows with nb_min_cols columns

                if filtered_rows:
                    LOGGER.debug(f"--- Page {page_number + 1}, Table {i + 1} ---")
                    LOGGER.debug(
                        f"Table with {len(filtered_rows)} rows with minimum {nb_min_cols} columns"
                    )
                    for row in filtered_rows:
                        LOGGER.debug(
                            f"({len(row)} columns) {row}"
                        )  # Display the size of each row
                        row_data = [
                            pdf_name,
                            page_number + 1,
                            reference_date,
                        ] + row
                        table_data.append(row_data)
                    LOGGER.debug("\n" + "=" * 50 + "\n")
                else:
                    LOGGER.debug(
                        f"No rows with nb_min_cols columns found on page {page_number + 1}, table {i + 1}, {len(table[0])} columns\n===\n{table}\n==="
                    )

    if len(table_data) == 0:
        table_data = None

    return [
        _clip_or_pad_list(
            input_list=row,
            desired_number_of_elements=expected_number_of_columns,
            padding_value=None
        ) for row in table_data
    ]


def mhirj_supplier_list_post_processing(df: pd.DataFrame) -> pd.DataFrame:
    """
    Process the raw parsed sommier data :

    - strip str columns of newlines, whitespaces, etc.
    - extract the "extraction" timestamp
    - reorder columns

    :param df: A DataFrame with the parsed contents from mhirj supplier list files. It must contain
        the columns "pdf_name", "page_number", "reference_date", "vendor_code_1",
        "vendor_code_2", "vendor_name", "address", "class", "specification_controlled_number", "specification_controlled_limitation", "general_limitation".
    :type df: pd.DataFrame

    :return: A copy of the original DataFrame with processed columns
    :rtype: pd.DataFrame

    """

    if "reference_date" in df.columns:
        # 🔹 Nettoyer les suffixes st/nd/rd/th
        df["_meta_extraction_timestamp"] = (
            df["reference_date"]
            .str.replace(r"(\d+)(st|nd|rd|th)", r"\1", regex=True)
            .pipe(pd.to_datetime, format="%B %d, %Y", errors="coerce")
        )
    else:
        df["_meta_extraction_timestamp"] = pd.NaT

    df["_meta_ingestion_timestamp"] = datetime.now()

    if "pdf_name" in df.columns:
        df["_meta_file_name"] = df["pdf_name"]
        df = df.drop(columns=["pdf_name"])
    else:
        df["_meta_file_name"] = None

    # Remove newlines in specific columns
    df = _remove_newlines_from_textual_columns(
        df=df,
        columns=["vendor_name", "address", "class", "specification_controlled_number", "specification_controlled_limitation", "general_limitation"],
    )

    # columns reordering
    return _put_columns_first(
        df=df,
        columns_to_put_first=[
            "_meta_extraction_timestamp",
            "_meta_ingestion_timestamp",
            "_meta_file_name",
        ],
    )