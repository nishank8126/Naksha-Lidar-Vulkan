"""
NakshaApp DGN Conversion Suite v1.3.4
DGN -> SNT / DGN -> DXF / SNT -> DXF converter.

Usage:
    from nakshaapp_dgn_converter.snt_pipeline import convert as convert_to_snt
    from nakshaapp_dgn_converter.dxf_pipeline import convert_dgn_to_dxf as convert_to_dxf
    from nakshaapp_dgn_converter.snt_to_dxf_pipeline import _guarded_reverse_convert as convert_snt_to_dxf

CLI:
    dgn-to-snt input.dgn output.snt
    dgn-to-dxf input.dgn output.dxf
    snt-to-dxf input.snt output.dxf
"""
__version__ = "1.3.4"
__author__  = "NakshaTech Software Team"
__license__ = "MIT"
