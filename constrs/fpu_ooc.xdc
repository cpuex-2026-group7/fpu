# 80 MHz クロック (周期 12.5 ns)
create_clock -name clk -period 12.500 [get_ports clk]

# 入力ポート制約 (入口 FF の前に組み合わせ回路を挟んでいないため 0.0 ns、
# あるいはコア側の組み合わせ出力を想定して 5.0 ns にしても入口 FF 直結なら余裕で通る)
set_input_delay  -clock clk 0.0 [remove_from_collection [all_inputs] [get_ports clk]]

# README.md 制約: 最終 FF → 出力ポートを半サイクル以内に収める
set_output_delay -clock clk 5.0 [all_outputs]