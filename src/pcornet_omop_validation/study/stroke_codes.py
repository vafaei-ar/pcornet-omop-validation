from __future__ import annotations

"""Prespecified ischemic-stroke code set used by the PSU PROMIS phenotype.

Codes are normalized by removing decimal points before comparison. The ICD-9 set
contains the nine 433.x1/434.x1 infarction codes. The ICD-10 set reproduces the
explicit PSU phenotype configuration rather than using an I63 prefix rule.
"""

ICD9_STROKE_CODES = frozenset(
    {
        "43301", "43311", "43321", "43331", "43381", "43391",
        "43401", "43411", "43491",
    }
)

ICD10_STROKE_CODES = frozenset(
    {
        "H341",
        "I63", "I630", "I6300", "I6301", "I63011", "I63012", "I63013", "I63019", "I6302", "I6303", "I63031", "I63032", "I63033", "I63039", "I6309",
        "I631", "I6310", "I6311", "I63111", "I63112", "I63113", "I63119", "I6312", "I6313", "I63131", "I63132", "I63133", "I63139", "I6319",
        "I632", "I6320", "I6321", "I63211", "I63212", "I63213", "I63219", "I6322", "I6323", "I63231", "I63232", "I63233", "I63239", "I6329",
        "I633", "I6330", "I6331", "I63311", "I63312", "I63313", "I63319", "I6332", "I63321", "I63322", "I63323", "I63329", "I6333", "I63331", "I63332", "I63333", "I63339", "I6334", "I63341", "I63342", "I63343", "I63349", "I6339",
        "I634", "I6340", "I6341", "I63411", "I63412", "I63413", "I63419", "I6342", "I63421", "I63422", "I63423", "I63429", "I6343", "I63431", "I63432", "I63433", "I63439", "I6344", "I63441", "I63442", "I63443", "I63449", "I6349",
        "I635", "I6350", "I6351", "I63511", "I63512", "I63513", "I63519", "I6352", "I63521", "I63522", "I63523", "I63529", "I6353", "I63531", "I63532", "I63533", "I63539", "I6354", "I63541", "I63542", "I63543", "I63549", "I6359",
        "I636", "I638", "I6381", "I6389", "I639",
    }
)

ICD9_TYPES = frozenset({"09", "9", "ICD9", "ICD9CM"})
ICD10_TYPES = frozenset({"10", "ICD10", "ICD10CM"})
PRIMARY_PDX_VALUES = frozenset({"P"})
ELIGIBLE_ENC_TYPES = frozenset({"EI", "IP"})
